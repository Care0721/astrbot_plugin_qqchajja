"""
采销助手插件 for AstrBot
功能：
- 群聊/私聊自动识别求购、供应、询价消息
- 销售侧：匹配库存商品，自动/手动报价，记录报价历史
- 采购侧：记录供应商报价，聚合比价，一键联系
- 报价模板管理，自动报价规则
- 管理员查看所有报价/供应记录
"""

import json
import re
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple

from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Plain

logger = logging.getLogger("astrbot_plugin_sales_purchase")

# ======================== 常量与正则 ========================
CODE_PATTERN = re.compile(r'([A-Za-z0-9]+[-][A-Za-z0-9]+[-]?\d*)')
PRICE_PATTERN = re.compile(r'(\d+(?:\.\d+)?)\s*元/?个?|价格[:：]?\s*(\d+(?:\.\d+)?)')
QUANTITY_PATTERN = re.compile(r'(\d+)\s*个|数量[:：]?\s*(\d+)')

# 消息分类关键词
PURCHASE_KEYWORDS = ['求购', '收购', '需要', '买']
SUPPLY_KEYWORDS = ['供应', '出售', '提供', '有货', '报价']
INQUIRY_KEYWORDS = ['询价', '多少钱', '价格', '报价多少']

@register("sales_purchase_assistant", "你的名字", "采销助手：自动抓取求购/供应，智能匹配报价", "2.0.0")
class SalesPurchaseAssistant(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.db_path = Path(__file__).parent / "sp_assistant.db"
        self._init_database()
        
        # 从配置获取监听群组、管理员ID等，如果配置不存在则使用默认值
        self.target_groups = self._get_config("target_groups", [])
        # ✅ 默认管理员ID已加入：3524815759
        self.admin_ids = self._get_config("admin_ids", ["3524815759"])
        self.auto_quote_enabled = self._get_config("auto_quote_enabled", False)
        self.default_template_id = self._get_config("default_template_id", 1)
        
        logger.info(f"采销助手已加载，监听群组: {self.target_groups}, 自动报价: {self.auto_quote_enabled}, 管理员: {self.admin_ids}")

    # ======================== 配置读取 ========================
    def _get_config(self, key: str, default=None):
        try:
            config = self.context.get_plugin_config()
            return config.get(key, default)
        except:
            return default

    # ======================== 数据库初始化 ========================
    def _init_database(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # 1. 库存表（销售侧）
        c.execute('''CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            product_name TEXT,
            quantity INTEGER DEFAULT 0,
            price REAL,
            supplier_qq TEXT,
            supplier_name TEXT,
            extra TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # 2. 求购记录表（原始消息）
        c.execute('''CREATE TABLE IF NOT EXISTS purchase_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            content TEXT,
            customer_qq TEXT,
            customer_name TEXT,
            group_id TEXT,
            quantity INTEGER,
            status TEXT DEFAULT 'pending',  -- pending, quoted, matched
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # 3. 报价记录表
        c.execute('''CREATE TABLE IF NOT EXISTS quote_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER,
            inventory_id INTEGER,
            customer_qq TEXT,
            customer_name TEXT,
            product_code TEXT,
            product_name TEXT,
            quantity INTEGER,
            price REAL,
            total_amount REAL,
            quote_type TEXT,     -- auto / manual
            quote_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'sent',
            operator_id TEXT     -- 操作员QQ
        )''')
        
        # 4. 供应信息表（采购侧）
        c.execute('''CREATE TABLE IF NOT EXISTS supply_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_qq TEXT,
            supplier_name TEXT,
            product_code TEXT,
            product_name TEXT,
            quantity INTEGER,
            price REAL,
            original_content TEXT,
            recorded_by TEXT,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # 5. 报价模板表
        c.execute('''CREATE TABLE IF NOT EXISTS quote_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            content TEXT NOT NULL,
            variables TEXT,   -- JSON array
            created_by TEXT
        )''')
        # 插入默认模板
        c.execute("SELECT COUNT(*) FROM quote_templates")
        if c.fetchone()[0] == 0:
            default_content = "【报价】\n商品编码：{code}\n商品名称：{name}\n数量：{quantity}\n单价：{price}元\n总价：{total}元\n如有需要请联系我。"
            c.execute("INSERT INTO quote_templates (name, content, variables) VALUES (?, ?, ?)",
                      ("标准模板", default_content, json.dumps(["code","name","quantity","price","total"])))
        
        # 6. 自动报价规则表
        c.execute('''CREATE TABLE IF NOT EXISTS auto_quote_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_name TEXT,
            match_code_pattern TEXT,   -- regex
            template_id INTEGER,
            min_price REAL,
            max_price REAL,
            enabled INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 0
        )''')
        
        # 7. 用户表（与Web共享）
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            qq_id TEXT UNIQUE,
            role TEXT DEFAULT 'user',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # 8. 配置表（存储插件运行时配置）
        c.execute('''CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')
        # 读取默认模板ID
        c.execute("SELECT value FROM config WHERE key='default_template_id'")
        row = c.fetchone()
        if row:
            self.default_template_id = int(row[0])
        
        conn.commit()
        conn.close()
        logger.info("数据库初始化完成")

    # ======================== 消息分类 ========================
    def classify_message(self, text: str) -> str:
        """返回 purchase / supply / inquiry / other"""
        text_lower = text.lower()
        if any(kw in text_lower for kw in PURCHASE_KEYWORDS):
            return 'purchase'
        if any(kw in text_lower for kw in SUPPLY_KEYWORDS):
            return 'supply'
        if any(kw in text_lower for kw in INQUIRY_KEYWORDS):
            return 'inquiry'
        # 纯编码也视为求购
        if CODE_PATTERN.fullmatch(text.strip()):
            return 'purchase'
        return 'other'

    def extract_codes(self, text: str) -> List[str]:
        return CODE_PATTERN.findall(text)
    
    def extract_price(self, text: str) -> Optional[float]:
        m = PRICE_PATTERN.search(text)
        if m:
            price_str = m.group(1) or m.group(2)
            return float(price_str)
        return None
    
    def extract_quantity(self, text: str) -> Optional[int]:
        m = QUANTITY_PATTERN.search(text)
        if m:
            qty_str = m.group(1) or m.group(2)
            return int(qty_str)
        return None

    # ======================== 库存匹配 ========================
    def match_inventory(self, code: str) -> Optional[Dict]:
        """根据编码匹配库存（返回第一个匹配项）"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT id, code, product_name, quantity, price, supplier_qq, supplier_name FROM inventory WHERE code LIKE ? AND quantity > 0",
                  (f'%{code}%',))
        row = c.fetchone()
        conn.close()
        if row:
            return {
                'id': row[0],
                'code': row[1],
                'product_name': row[2],
                'quantity': row[3],
                'price': row[4],
                'supplier_qq': row[5],
                'supplier_name': row[6]
            }
        return None

    def match_supply_records(self, code: str) -> List[Dict]:
        """采购侧：匹配供应记录（用于比价）"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''SELECT supplier_name, supplier_qq, product_code, product_name, quantity, price, recorded_at
                     FROM supply_records WHERE product_code LIKE ? ORDER BY price ASC''', (f'%{code}%',))
        rows = c.fetchall()
        conn.close()
        return [{
            'supplier_name': r[0],
            'supplier_qq': r[1],
            'product_code': r[2],
            'product_name': r[3],
            'quantity': r[4],
            'price': r[5],
            'recorded_at': r[6]
        } for r in rows]

    # ======================== 报价模板与渲染 ========================
    def get_quote_template(self, template_id: int = None) -> Optional[Dict]:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        if template_id:
            c.execute("SELECT id, name, content, variables FROM quote_templates WHERE id=?", (template_id,))
        else:
            c.execute("SELECT id, name, content, variables FROM quote_templates ORDER BY id LIMIT 1")
        row = c.fetchone()
        conn.close()
        if row:
            return {'id': row[0], 'name': row[1], 'content': row[2], 'variables': json.loads(row[3]) if row[3] else []}
        return None

    def render_quote(self, template: Dict, inventory: Dict, customer_name: str, quantity: int = 1) -> str:
        total = inventory['price'] * quantity if inventory['price'] else 0
        content = template['content']
        replacements = {
            '{code}': inventory['code'],
            '{name}': inventory.get('product_name', '商品'),
            '{price}': str(inventory['price']) if inventory['price'] else '待询',
            '{quantity}': str(quantity),
            '{total}': str(total),
            '{customer}': customer_name
        }
        for k, v in replacements.items():
            content = content.replace(k, v)
        return content

    # ======================== 报价执行 ========================
    async def send_quote(self, event: AstrMessageEvent, target_qq: str, message: str, is_group: bool = True):
        """发送报价消息，群聊中@对方，私聊直接发送"""
        if is_group:
            # 群聊中@目标用户（需要对方在群内）
            chain = MessageChain([At(qq=target_qq), Plain(f"\n{message}")])
        else:
            # 私聊直接发送文本
            chain = MessageChain([Plain(message)])
        yield event.reply(chain)
    
    async def auto_quote_for_request(self, event: AstrMessageEvent, request_id: int, code: str, 
                                     customer_qq: str, customer_name: str, quantity: int = 1):
        """自动报价主逻辑"""
        if not self.auto_quote_enabled:
            return False
        
        inventory = self.match_inventory(code)
        if not inventory:
            return False
        
        # 检查自动报价规则（简单实现：只要库存价格>0就报价）
        # 可扩展按规则表判断
        if inventory['price'] is None or inventory['price'] <= 0:
            return False
        
        template = self.get_quote_template(self.default_template_id)
        if not template:
            return False
        
        quote_msg = self.render_quote(template, inventory, customer_name, quantity)
        # 判断消息来源是群还是私聊
        is_group = event.get_group_id() is not None
        await self.send_quote(event, customer_qq, quote_msg, is_group)
        
        # 记录报价
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''INSERT INTO quote_records 
                     (request_id, inventory_id, customer_qq, customer_name, product_code, product_name, 
                      quantity, price, total_amount, quote_type, operator_id)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (request_id, inventory['id'], customer_qq, customer_name, inventory['code'],
                   inventory['product_name'], quantity, inventory['price'], inventory['price']*quantity,
                   'auto', None))
        conn.commit()
        conn.close()
        return True

    # ======================== 保存求购/供应记录 ========================
    def save_purchase_request(self, code: str, content: str, customer_qq: str, customer_name: str, 
                              group_id: str = None, quantity: int = None) -> int:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''INSERT INTO purchase_requests (code, content, customer_qq, customer_name, group_id, quantity, status)
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (code, content, customer_qq, customer_name, group_id, quantity, 'pending'))
        rid = c.lastrowid
        conn.commit()
        conn.close()
        return rid

    def save_supply_record(self, supplier_qq: str, supplier_name: str, product_code: str, 
                           product_name: str, quantity: int, price: float, original_content: str, operator_qq: str):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''INSERT INTO supply_records 
                     (supplier_qq, supplier_name, product_code, product_name, quantity, price, original_content, recorded_by)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (supplier_qq, supplier_name, product_code, product_name, quantity, price, original_content, operator_qq))
        conn.commit()
        conn.close()

    # ======================== 命令处理 ========================
    @command("quote")
    async def manual_quote(self, event: AstrMessageEvent, code_or_id: str = None):
        """手动报价：/quote 商品编码 或 /quote 库存ID"""
        if not code_or_id:
            yield event.plain_result("用法：/quote 商品编码 或 /quote 库存ID")
            return
        # 尝试作为库存ID查询
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT id, code, product_name, price, supplier_qq, supplier_name FROM inventory WHERE id=? OR code=?", 
                  (code_or_id, code_or_id))
        inv = c.fetchone()
        conn.close()
        if not inv:
            yield event.plain_result(f"未找到商品编码或ID：{code_or_id}")
            return
        inventory = {'id': inv[0], 'code': inv[1], 'product_name': inv[2], 'price': inv[3], 
                     'supplier_qq': inv[4], 'supplier_name': inv[5]}
        template = self.get_quote_template(self.default_template_id)
        if not template:
            yield event.plain_result("未找到报价模板")
            return
        # 获取对方QQ（私聊时取对方，群聊时需指定）
        is_group = event.get_group_id() is not None
        if is_group:
            yield event.plain_result("群聊中使用 /quote 需要 @对方 或提供客户QQ，暂不支持，请私聊机器人使用此命令。")
            return
        else:
            customer_qq = event.get_sender_id()
            customer_name = event.get_sender_name() or customer_qq
            quote_msg = self.render_quote(template, inventory, customer_name, 1)
            await self.send_quote(event, customer_qq, quote_msg, False)
            # 记录报价（request_id为0表示手动报价无关联求购）
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('''INSERT INTO quote_records 
                         (inventory_id, customer_qq, customer_name, product_code, product_name, price, total_amount, quote_type, operator_id)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (inventory['id'], customer_qq, customer_name, inventory['code'], inventory['product_name'],
                       inventory['price'], inventory['price'], 'manual', event.get_sender_id()))
            conn.commit()
            conn.close()
            yield event.plain_result("报价已发送并记录")

    @command("quote_history")
    async def quote_history(self, event: AstrMessageEvent, code: str = None, customer: str = None):
        """查看报价历史：/quote_history [商品编码] [客户名]"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可查看报价历史")
            return
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        query = "SELECT quote_time, customer_name, product_code, product_name, price, quantity, total_amount, quote_type FROM quote_records WHERE 1=1"
        params = []
        if code:
            query += " AND product_code LIKE ?"
            params.append(f'%{code}%')
        if customer:
            query += " AND customer_name LIKE ?"
            params.append(f'%{customer}%')
        query += " ORDER BY quote_time DESC LIMIT 20"
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()
        if not rows:
            yield event.plain_result("暂无报价记录")
            return
        msg = "📋 报价历史（最近20条）：\n\n"
        for r in rows:
            msg += f"{r[0][:16]} | {r[1]} | {r[2]} | {r[3]} | 单价:{r[4]} | 数量:{r[5]} | 总价:{r[6]} | {r[7]}\n"
        yield event.plain_result(msg)

    @command("supply_list")
    async def supply_list(self, event: AstrMessageEvent, code: str = None):
        """查看供应记录（聚合比价）：/supply_list [商品编码]"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可查看供应列表")
            return
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        if code:
            c.execute("SELECT product_code, supplier_name, price, quantity, recorded_at FROM supply_records WHERE product_code LIKE ? ORDER BY price ASC", (f'%{code}%',))
        else:
            c.execute("SELECT product_code, supplier_name, price, quantity, recorded_at FROM supply_records ORDER BY recorded_at DESC LIMIT 30")
        rows = c.fetchall()
        conn.close()
        if not rows:
            yield event.plain_result("暂无供应记录")
            return
        msg = "📦 供应记录（低价优先）：\n\n"
        for r in rows:
            msg += f"{r[0]} | {r[1]} | 单价:{r[2]} | 数量:{r[3]} | {r[4][:16]}\n"
        yield event.plain_result(msg)

    @command("add_inventory")
    async def add_inventory(self, event: AstrMessageEvent, code: str, name: str = "", price: float = None, qty: int = 1):
        """添加库存：/add_inventory 编码 商品名 价格 数量"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可添加库存")
            return
        if not code:
            yield event.plain_result("用法：/add_inventory 编码 [商品名] [价格] [数量]")
            return
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO inventory (code, product_name, price, quantity, supplier_qq, supplier_name) VALUES (?, ?, ?, ?, ?, ?)",
                  (code, name, price, qty, event.get_sender_id(), event.get_sender_name()))
        conn.commit()
        conn.close()
        yield event.plain_result(f"✅ 已添加库存：{code} {name} 价格:{price} 数量:{qty}")

    @command("template_list")
    async def template_list(self, event: AstrMessageEvent):
        """查看报价模板列表"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT id, name, content FROM quote_templates")
        rows = c.fetchall()
        conn.close()
        if not rows:
            yield event.plain_result("暂无报价模板")
            return
        msg = "📄 报价模板列表：\n"
        for r in rows:
            msg += f"[{r[0]}] {r[1]}\n内容：{r[2][:50]}...\n\n"
        yield event.plain_result(msg)

    @command("set_template")
    async def set_template(self, event: AstrMessageEvent, template_id: int):
        """设置当前使用的报价模板（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限不足")
            return
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT id FROM quote_templates WHERE id=?", (template_id,))
        if not c.fetchone():
            conn.close()
            yield event.plain_result("模板ID不存在")
            return
        # 保存到配置表
        c.execute("REPLACE INTO config (key, value) VALUES (?, ?)", ("default_template_id", str(template_id)))
        conn.commit()
        conn.close()
        self.default_template_id = template_id
        yield event.plain_result(f"已设置默认报价模板ID: {template_id}")

    # ======================== 消息监听（核心） ========================
    async def on_message(self, event: AstrMessageEvent):
        try:
            group_id = event.get_group_id()
            is_group = group_id is not None
            # 群聊时检查是否监听该群
            if is_group and self.target_groups and str(group_id) not in [str(g) for g in self.target_groups]:
                return
            
            text = event.get_plain_text()
            if not text:
                return
            
            msg_type = self.classify_message(text)
            sender_id = event.get_sender_id()
            sender_name = event.get_sender_name() or sender_id
            
            # 1. 销售侧：处理求购/询价
            if msg_type in ('purchase', 'inquiry'):
                codes = self.extract_codes(text)
                if not codes:
                    return
                code = codes[0]
                quantity = self.extract_quantity(text) or 1
                # 保存求购请求
                request_id = self.save_purchase_request(code, text, sender_id, sender_name, group_id, quantity)
                # 尝试自动报价
                if msg_type == 'purchase' or (msg_type == 'inquiry' and self.auto_quote_enabled):
                    success = await self.auto_quote_for_request(event, request_id, code, sender_id, sender_name, quantity)
                    if success:
                        # 自动报价后不再发送手动提示
                        return
                # 未自动报价则发送匹配提示
                inventory = self.match_inventory(code)
                if inventory:
                    msg = f"🔍 收到{ '求购' if msg_type=='purchase' else '询价' }：{code}\n"
                    msg += f"📦 匹配到库存：{inventory['code']} {inventory['product_name']}\n"
                    if inventory['price']:
                        msg += f"💰 参考价：{inventory['price']}元\n"
                    msg += f"📞 供应商QQ：{inventory['supplier_qq']}\n"
                    msg += "💡 如需报价，请私聊机器人使用 /quote 命令"
                    yield event.plain_result(msg)
                else:
                    # 无匹配库存，仅提示已记录
                    yield event.plain_result(f"✅ 已记录{ '求购' if msg_type=='purchase' else '询价' }信息：{code}")
            
            # 2. 采购侧：处理供应信息
            elif msg_type == 'supply':
                codes = self.extract_codes(text)
                if not codes:
                    return
                code = codes[0]
                price = self.extract_price(text)
                quantity = self.extract_quantity(text) or 0
                product_name = ""  # 可从文本中简单提取，暂留空
                # 保存供应记录
                self.save_supply_record(sender_id, sender_name, code, product_name, quantity, price, text, sender_id)
                # 可选：触发比价提示（聚合该商品的所有报价）
                supplies = self.match_supply_records(code)
                if len(supplies) >= 2:
                    min_price = supplies[0]['price']
                    min_supplier = supplies[0]['supplier_name']
                    reply = f"✅ 已记录供应商报价：{code} 价格 {price}元\n"
                    reply += f"📊 当前该商品最低报价：{min_price}元（来自 {min_supplier}）"
                    yield event.plain_result(reply)
                else:
                    yield event.plain_result(f"✅ 已记录供应信息：{code} 价格 {price}元")
            
            # 3. 其他类型忽略
            else:
                pass
                
        except Exception as e:
            logger.error(f"消息处理异常: {e}", exc_info=True)

    # ======================== 辅助方法 ========================
    def _is_admin(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id())
        return sender in [str(aid) for aid in self.admin_ids]