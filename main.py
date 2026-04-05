import json
import re
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent

logger = logging.getLogger("astrbot_plugin_purchase_match")

# 求购信息正则匹配（支持多种格式）
PURCHASE_PATTERNS = [
    r'^(\d+[-]\d+[-]\d+)$',           # 1-96885-3
    r'^(\d+[-]\d+)$',                  # 2350495-3
    r'^(\d+[-]\d+[-]\d+)$',            # 705-749-515
    r'^([A-Za-z0-9]+[-][A-Za-z0-9]+[-]?\d*)$',  # 通用格式
]

@register("purchase_match", "你的名字", "QQ求购信息采集与匹配插件", "1.0.0")
class PurchaseMatchPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 数据库路径
        self.db_path = Path(__file__).parent / "purchase_data.db"
        self._init_database()
        
        # 获取配置
        self.target_groups = self._get_config("target_groups", [])
        self.enable_auto_save = self._get_config("enable_auto_save", True)
        
        logger.info(f"求购匹配插件已加载，监听群组: {self.target_groups}")

    def _get_config(self, key: str, default=None):
        """获取插件配置"""
        try:
            config = self.context.get_plugin_config()
            return config.get(key, default)
        except:
            return default

    def _init_database(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 求购信息表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,              -- 求购编码
                content TEXT NOT NULL,           -- 原始消息内容
                sender_id TEXT NOT NULL,         -- 发送者ID
                sender_name TEXT,                -- 发送者昵称
                group_id TEXT,                   -- 群ID
                group_name TEXT,                 -- 群名称
                create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_matched INTEGER DEFAULT 0,    -- 是否已匹配
                matched_to TEXT                  -- 匹配到的库存ID
            )
        ''')
        
        # 用户注册信息表（与网站共享）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                email TEXT,
                qq_id TEXT UNIQUE,               -- QQ号，用于关联
                role TEXT DEFAULT 'user',        -- admin/user
                register_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 库存表（会员上传）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,        -- 关联users.id
                code TEXT NOT NULL,              -- 库存编码
                product_name TEXT,               -- 产品名称
                quantity INTEGER DEFAULT 0,      -- 数量
                price REAL,                      -- 价格
                extra_info TEXT,                 -- 扩展信息（JSON）
                upload_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')
        
        # 匹配记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_id INTEGER NOT NULL,
                inventory_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,         -- 匹配到的卖家用户ID
                match_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending',    -- pending/accepted/rejected
                FOREIGN KEY (purchase_id) REFERENCES purchases(id),
                FOREIGN KEY (inventory_id) REFERENCES inventory(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info("数据库初始化完成")

    def _is_purchase_message(self, text: str) -> bool:
        """判断是否为求购消息"""
        text = text.strip()
        for pattern in PURCHASE_PATTERNS:
            if re.match(pattern, text, re.IGNORECASE):
                return True
        # 检测"询价"关键词
        if "询价" in text:
            # 提取可能的编码部分
            codes = re.findall(r'[\d\-]+', text)
            if codes:
                return True
        return False

    def _extract_codes(self, text: str) -> List[str]:
        """提取消息中的编码"""
        codes = re.findall(r'[\d\-]+', text)
        return [code for code in codes if len(code) >= 3]

    def _save_purchase(self, code: str, content: str, sender_id: str, 
                       sender_name: str, group_id: str, group_name: str):
        """保存求购信息到数据库"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 检查是否已存在（防止重复）
        cursor.execute(
            "SELECT id FROM purchases WHERE content = ? AND sender_id = ? AND create_time > datetime('now', '-1 hour')",
            (content, sender_id)
        )
        existing = cursor.fetchone()
        if existing:
            conn.close()
            return existing[0]
        
        cursor.execute('''
            INSERT INTO purchases (code, content, sender_id, sender_name, group_id, group_name)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (code, content, sender_id, sender_name, group_id, group_name))
        
        purchase_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        logger.info(f"保存求购信息: {code} from {sender_name}")
        
        # 触发自动匹配
        self._auto_match(purchase_id, code)
        
        return purchase_id

    def _auto_match(self, purchase_id: int, code: str):
        """自动匹配求购与库存"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 查找匹配的库存
        cursor.execute('''
            SELECT i.id, i.user_id, i.code, u.username, u.qq_id
            FROM inventory i
            JOIN users u ON i.user_id = u.id
            WHERE i.code LIKE ? OR ? LIKE '%' || i.code || '%'
        ''', (f'%{code}%', code))
        
        matches = cursor.fetchall()
        
        for match in matches:
            inventory_id, user_id, inv_code, username, qq_id = match
            # 检查是否已有匹配记录
            cursor.execute(
                "SELECT id FROM matches WHERE purchase_id = ? AND inventory_id = ?",
                (purchase_id, inventory_id)
            )
            if not cursor.fetchone():
                cursor.execute('''
                    INSERT INTO matches (purchase_id, inventory_id, user_id, status)
                    VALUES (?, ?, ?, 'pending')
                ''', (purchase_id, inventory_id, user_id))
                
                # 更新求购的匹配状态
                cursor.execute(
                    "UPDATE purchases SET is_matched = 1, matched_to = ? WHERE id = ?",
                    (inventory_id, purchase_id)
                )
        
        conn.commit()
        conn.close()

    # ==================== 核心命令 ====================
    
    @command("purchase_match")
    async def manual_match(self, event: AstrMessageEvent, code: str = None):
        """手动匹配：/purchase_match [编码]"""
        if code:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT u.username, u.qq_id, i.code, i.product_name
                FROM inventory i
                JOIN users u ON i.user_id = u.id
                WHERE i.code = ? OR i.code LIKE ?
            ''', (code, f'%{code}%'))
            results = cursor.fetchall()
            conn.close()
            
            if results:
                msg = f"🔍 编码 [{code}] 的匹配结果：\n\n"
                for username, qq_id, inv_code, product_name in results:
                    msg += f"📦 {inv_code} - {product_name or '无名称'}\n"
                    msg += f"👤 卖家：{username}\n"
                    if qq_id:
                        msg += f"📞 QQ：{qq_id}\n"
                    msg += "\n"
                yield event.plain_result(msg)
            else:
                yield event.plain_result(f"❌ 未找到编码 [{code}] 的匹配库存")
        else:
            yield event.plain_result(
                "📋 求购匹配插件使用说明：\n\n"
                "1. 在群内发送求购信息（如 1-96885-3），系统自动采集\n"
                "2. 访问网站注册会员后上传库存，系统自动匹配\n"
                "3. 手动查询：/purchase_match 编码"
            )

    @command("purchase_list")
    async def list_purchases(self, event: AstrMessageEvent, page: int = 1):
        """查看最近的求购信息：/purchase_list [页码]"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员可查看")
            return
        
        per_page = 10
        offset = (page - 1) * per_page
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, code, content, sender_name, create_time, is_matched
            FROM purchases
            ORDER BY create_time DESC
            LIMIT ? OFFSET ?
        ''', (per_page, offset))
        results = cursor.fetchall()
        
        cursor.execute("SELECT COUNT(*) FROM purchases")
        total = cursor.fetchone()[0]
        conn.close()
        
        if not results:
            yield event.plain_result("📭 暂无求购记录")
            return
        
        msg = f"📋 求购列表（第{page}页，共{total}条）\n\n"
        for pid, code, content, sender, create_time, matched in results:
            status = "✅已匹配" if matched else "⏳待匹配"
            msg += f"[{pid}] {code} | {status}\n"
            msg += f"  来自：{sender} | {create_time[:16]}\n\n"
        
        yield event.plain_result(msg)

    # ==================== 消息监听（核心） ====================
    
    async def on_message(self, event: AstrMessageEvent):
        """监听所有群消息"""
        try:
            # 获取消息来源
            group_id = event.get_group_id()
            if not group_id:
                return  # 非群消息忽略
            
            # 检查是否在监听列表
            if self.target_groups and str(group_id) not in [str(g) for g in self.target_groups]:
                return
            
            # 获取消息内容
            message_str = event.get_plain_text()
            if not message_str:
                return
            
            # 判断是否为求购消息
            if not self._is_purchase_message(message_str):
                return
            
            # 提取编码
            codes = self._extract_codes(message_str)
            if not codes:
                return
            
            # 获取发送者信息
            sender_id = event.get_sender_id()
            sender_name = event.get_sender_name() or sender_id
            
            # 保存求购信息
            for code in codes:
                self._save_purchase(
                    code=code,
                    content=message_str,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    group_id=group_id,
                    group_name=event.get_group_name() or str(group_id)
                )
            
            # 可选：发送确认消息（避免刷屏，建议关闭）
            # yield event.plain_result(f"✅ 已记录求购信息：{codes[0]}")
            
        except Exception as e:
            logger.error(f"处理消息失败: {e}")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """管理员判断"""
        sender_id = str(event.get_sender_id())
        admin_ids = self._get_config("admin_ids", ["12345678"])
        return sender_id in [str(aid) for aid in admin_ids]