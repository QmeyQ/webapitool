"""
config_engine.py - 配置引擎

职责：
- 站点配置加载/保存
- AI 自动识别页面选择器
- 选择器验证与修复
- 配置文件热更新与快照管理
"""

import json
import os
import re
import time
import logging
import copy  # Added for snapshot
from typing import Dict, Optional, List, Any
from urllib import request, error
import bs4
from bs4 import BeautifulSoup

from data_models import SiteConfig, WorkflowStep, AIAnalysisResult


# ================= 常量配置 =================

class ConfigConstants:
    """配置引擎常量"""
    # 文件配置
    CONFIG_FILE = os.getenv("SITES_CONFIG_FILE", "sites.json")
    
    # AI 配置（支持环境变量）
    HELPER_API_KEY = os.getenv("HELPER_API_KEY", "lumingya")
    HELPER_BASE_URL = os.getenv("HELPER_BASE_URL", "http://127.0.0.1:5104/v1")
    HELPER_MODEL = os.getenv("HELPER_MODEL", "gemini-3-pro")
    
    # HTML 处理
    MAX_HTML_CHARS = int(os.getenv("MAX_HTML_CHARS", "120000"))
    TEXT_TRUNCATE_LENGTH = 80
    
    # AI 重试
    AI_MAX_RETRIES = 3
    AI_RETRY_BASE_DELAY = 1.0  # 初始延迟
    AI_RETRY_MAX_DELAY = 10.0  # 最大延迟
    AI_REQUEST_TIMEOUT = 120
    
    # 隐身模式站点
    STEALTH_DOMAINS = ['lmarena.ai', 'poe.com', 'you.com', 'chatgpt.com']


# 默认工作流
# 注意：STREAM_WAIT 步骤会使用 result_container 选择器
# 如果配置了 message_wrapper，会用于定位完整消息容器
# 如果配置了 generating_indicator，会用于检测生成状态
DEFAULT_WORKFLOW: List[WorkflowStep] = [
    {"action": "CLICK", "target": "new_chat_btn", "optional": True, "value": None},
    {"action": "WAIT", "target": "", "optional": False, "value": "0.5"},
    {"action": "FILL_INPUT", "target": "input_box", "optional": False, "value": None},
    {"action": "CLICK", "target": "send_btn", "optional": True, "value": None},
    {"action": "KEY_PRESS", "target": "Enter", "optional": True, "value": None},
    {"action": "STREAM_WAIT", "target": "result_container", "optional": False, "value": None}
]

# 通用回退选择器
FALLBACK_SELECTORS = {
    "input_box": "textarea",
    "send_btn": "button[type=\"submit\"]",
    "result_container": "div",
    "new_chat_btn": None,
    "message_wrapper": None,
    "generating_indicator": None,
    "upload_btn": None,                    # 新增
    "file_input": "input[type=\"file\"]",  # 新增
}

# 无效选择器语法模式
INVALID_SYNTAX_PATTERNS = [
    (r'~\s*\.\.', '~ .. 无效语法'),
    (r'\.\.\s*$', '结尾 .. 无效'),
    (r'>>\s', '>> 无效语法'),
    (r':has\(', ':has() 兼容性差'),
    (r'\s~\s*$', '结尾 ~ 无效'),
]


# ================= 日志配置 =================

logger = logging.getLogger('config_engine')
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s [Config] %(message)s', datefmt='%H:%M:%S')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ================= HTML 清理器 =================

class HTMLCleaner:
    """HTML 清理器 - 独立职责"""
    
    # 要移除的标签
    TAGS_TO_REMOVE = [
        'script', 'style', 'meta', 'link', 'noscript',
        'img', 'video', 'audio', 'iframe', 'canvas',
        'path', 'rect', 'circle', 'polygon', 'defs', 'clipPath',
        'header', 'footer', 'nav', 'aside',  # 移除非核心区域
    ]
    
    # 保留的属性
    ALLOWED_ATTRS = [
        'id', 'class', 'name', 'placeholder', 'aria-label', 'role',
        'data-testid', 'type', 'disabled', 'value', 'title', 'tabindex',
        'contenteditable', 'href'
    ]
    
    # 交互元素标签（必须保留）
    INTERACTIVE_TAGS = ['input', 'textarea', 'button', 'form', 'a']
    
    # 核心区域选择器（优先保留）
    CORE_AREA_SELECTORS = [
        '[role="main"]',
        'main',
        '#app',
        '#root',
        '.chat',
        '.conversation',
        '.message',
    ]
    
    def __init__(self, max_chars: int = None, text_truncate: int = None):
        self.max_chars = max_chars or ConfigConstants.MAX_HTML_CHARS
        self.text_truncate = text_truncate or ConfigConstants.TEXT_TRUNCATE_LENGTH
    
    def clean(self, html: str) -> str:
        """深度清理 HTML"""
        logger.debug("开始 HTML 清理...")
        original_length = len(html)
        
        soup = BeautifulSoup(html, 'html.parser')
        
        # 1. 提取所有交互元素（在删除任何内容前）
        interactive_elements = self._extract_interactive_elements(soup)
        
        # 2. 移除非必要标签
        for tag in soup(self.TAGS_TO_REMOVE):
            tag.decompose()
        
        # 3. 移除注释
        for element in soup(text=lambda t: isinstance(t, bs4.element.Comment)):
            element.extract()
        
        # 4. 清理属性和截断文本
        for tag in soup.find_all(True):
            if tag.string and len(tag.string) > self.text_truncate:
                tag.string = tag.string[:self.text_truncate] + "..."
            
            attrs = dict(tag.attrs)
            for attr in attrs:
                if attr not in self.ALLOWED_ATTRS:
                    del tag.attrs[attr]
            
            if 'class' in tag.attrs and isinstance(tag.attrs['class'], list):
                tag.attrs['class'] = " ".join(tag.attrs['class'])
        
        # 5. 获取清理后的 HTML
        clean_html = str(soup.body) if soup.body else str(soup)
        clean_html = re.sub(r'\s+', ' ', clean_html).strip()
        
        # 6. 智能截断（如果需要）
        if len(clean_html) > self.max_chars:
            logger.warning(f"HTML 过长 ({len(clean_html)})，执行智能截断...")
            clean_html = self._smart_truncate(clean_html, interactive_elements)
        
        final_length = len(clean_html)
        reduction = 100 - (final_length / original_length * 100) if original_length > 0 else 0
        logger.info(f"HTML 清理完成: {original_length} → {final_length} 字符 (减少 {reduction:.1f}%)")
        
        return clean_html
    
    def _extract_interactive_elements(self, soup: BeautifulSoup) -> str:
        """提取所有交互元素的 HTML 片段"""
        elements = []
        
        for tag_name in self.INTERACTIVE_TAGS:
            for element in soup.find_all(tag_name):
                # 获取元素及其父级上下文（保留 2 层父级）
                context = self._get_element_with_context(element, levels=2)
                if context:
                    elements.append(context)
        
        # 去重
        unique_elements = list(dict.fromkeys(elements))
        return "\n".join(unique_elements)
    
    def _get_element_with_context(self, element, levels: int = 2) -> str:
        """获取元素及其父级上下文"""
        try:
            # 向上找父级
            current = element
            for _ in range(levels):
                if current.parent and current.parent.name not in ['body', 'html', '[document]']:
                    current = current.parent
                else:
                    break
            
            # 简化输出：只保留关键属性
            html_str = str(current)
            # 截断过长的单个元素
            if len(html_str) > 2000:
                html_str = html_str[:2000] + "..."
            return html_str
        except Exception:
            return str(element)
    
    def _smart_truncate(self, html: str, interactive_html: str) -> str:
        """
        智能截断策略：
        1. 确保交互元素始终包含
        2. 在标签边界截断，不破坏 HTML 结构
        3. 保留首尾 + 中间核心区域
        """
        # 交互元素预算：最多占用 30% 空间
        interactive_budget = int(self.max_chars * 0.3)
        if len(interactive_html) > interactive_budget:
            interactive_html = interactive_html[:interactive_budget]
        
        # 剩余预算分配给主 HTML
        remaining_budget = self.max_chars - len(interactive_html) - 100  # 100 for markers
        
        if remaining_budget <= 0:
            # 交互元素已经超出预算，只返回交互元素
            logger.warning("交互元素已占满预算")
            return interactive_html
        
        # 尝试找到核心区域
        core_html = self._extract_core_area(html)
        if core_html and len(core_html) <= remaining_budget:
            # 核心区域在预算内，使用核心区域 + 交互元素
            result = core_html + "\n<!-- INTERACTIVE ELEMENTS -->\n" + interactive_html
            return result[:self.max_chars]
        
        # 回退：首尾各取一部分 + 交互元素
        head_budget = remaining_budget // 3
        tail_budget = remaining_budget // 3
        
        # 在标签边界截断
        head_part = self._truncate_at_tag_boundary(html[:head_budget * 2], head_budget, from_end=False)
        tail_part = self._truncate_at_tag_boundary(html[-tail_budget * 2:], tail_budget, from_end=True)
        
        result = (
            head_part +
            "\n<!-- TRUNCATED: MIDDLE SECTION -->\n" +
            "<!-- INTERACTIVE ELEMENTS START -->\n" +
            interactive_html +
            "\n<!-- INTERACTIVE ELEMENTS END -->\n" +
            tail_part
        )
        
        # 最终长度检查
        if len(result) > self.max_chars:
            result = result[:self.max_chars]
        
        return result
    
    def _extract_core_area(self, html: str) -> Optional[str]:
        """尝试提取核心区域"""
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            for selector in self.CORE_AREA_SELECTORS:
                try:
                    element = soup.select_one(selector)
                    if element:
                        core_html = str(element)
                        logger.debug(f"找到核心区域: {selector} ({len(core_html)} chars)")
                        return core_html
                except Exception:
                    continue
            
            return None
        except Exception as e:
            logger.debug(f"提取核心区域失败: {e}")
            return None
    
    def _truncate_at_tag_boundary(self, html: str, max_len: int, from_end: bool = False) -> str:
        """
        在标签边界截断，避免破坏 HTML 结构
        
        Args:
            html: HTML 字符串
            max_len: 最大长度
            from_end: True 表示从末尾开始保留
        """
        if len(html) <= max_len:
            return html
        
        if from_end:
            # 从末尾保留：找到合适的起始位置
            start_pos = len(html) - max_len
            # 向后找第一个 < 作为起始
            tag_start = html.find('<', start_pos)
            if tag_start != -1 and tag_start < len(html) - 100:
                return html[tag_start:]
            return html[-max_len:]
        else:
            # 从开头保留：找到合适的结束位置
            # 向前找最后一个 > 作为结束
            tag_end = html.rfind('>', 0, max_len)
            if tag_end != -1 and tag_end > 100:
                return html[:tag_end + 1]
            return html[:max_len]


# ================= 选择器验证器 =================

class SelectorValidator:
    """选择器验证器"""
    
    def validate(self, selectors: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
        """验证并修复选择器"""
        fixed = {}
        
        for key, selector in selectors.items():
            if selector is None:
                fixed[key] = FALLBACK_SELECTORS.get(key)
                continue
            
            is_invalid = False
            invalid_reason = ""
            
            for pattern, reason in INVALID_SYNTAX_PATTERNS:
                if re.search(pattern, selector):
                    is_invalid = True
                    invalid_reason = reason
                    break
            
            if is_invalid:
                logger.warning(f"❌ 无效选择器 [{key}]: {selector}")
                logger.warning(f"   原因: {invalid_reason}")
                
                repaired = self._try_repair(selector)
                if repaired:
                    logger.info(f"   ✅ 修复为: {repaired}")
                    fixed[key] = repaired
                else:
                    fallback = FALLBACK_SELECTORS.get(key)
                    logger.info(f"   🔄 回退为: {fallback}")
                    fixed[key] = fallback
            else:
                if re.search(r'\._[a-f0-9]{5,}|^\.[a-f0-9]{6,}', selector):
                    logger.info(f"ℹ️  哈希类名 [{key}]: {selector} (可能不稳定，但保留)")
                
                fixed[key] = selector
        
        return fixed
    
    def _try_repair(self, selector: str) -> Optional[str]:
        """尝试修复选择器"""
        tag_match = re.match(r'^(\w+)', selector)
        if not tag_match:
            return None
        
        tag = tag_match.group(1)
        
        attr_patterns = [
            r'(\[name=["\']?\w+["\']?\])',
            r'(\[type=["\']?\w+["\']?\])',
            r'(\[role=["\']?\w+["\']?\])',
            r'(#[\w-]+)',
        ]
        
        for pattern in attr_patterns:
            match = re.search(pattern, selector)
            if match:
                return tag + match.group(1)
        
        return tag


# ================= AI 分析器 =================

class AIAnalyzer:
    """AI 页面分析器"""
    
    def __init__(self):
        self.api_key = ConfigConstants.HELPER_API_KEY
        self.base_url = ConfigConstants.HELPER_BASE_URL.rstrip('/')
        self.model = ConfigConstants.HELPER_MODEL
        
        if not self.api_key:
            logger.warning("⚠️  未配置 HELPER_API_KEY，AI 分析功能将不可用")
    
    def analyze(self, html: str) -> Optional[Dict[str, str]]:
        """分析 HTML 并返回选择器"""
        if not self.api_key:
            logger.error("API Key 未配置")
            return None
        
        prompt = self._build_prompt(html)
        
        for attempt in range(ConfigConstants.AI_MAX_RETRIES):
            try:
                logger.info(f"正在请求 AI 分析（尝试 {attempt + 1}/{ConfigConstants.AI_MAX_RETRIES}）...")
                
                response = self._request_ai(prompt)
                if response:
                    selectors = self._extract_json(response)
                    if selectors:
                        logger.info("✅ AI 分析成功")
                        return selectors
                
                logger.warning(f"第 {attempt + 1} 次分析失败")
            
            except Exception as e:
                logger.error(f"AI 请求异常: {e}")
            
            if attempt < ConfigConstants.AI_MAX_RETRIES - 1:
                delay = min(
                    ConfigConstants.AI_RETRY_BASE_DELAY * (2 ** attempt),
                    ConfigConstants.AI_RETRY_MAX_DELAY
                )
                jitter = delay * 0.1 * (0.5 - os.urandom(1)[0] / 255)
                sleep_time = delay + jitter
                
                logger.info(f"等待 {sleep_time:.2f}s 后重试...")
                time.sleep(sleep_time)
        
        logger.error("❌ AI 分析失败（已达最大重试次数）")
        return None
    
    def _request_ai(self, prompt: str) -> Optional[str]:
        """向 AI API 发送请求"""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        data = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1
        }
        
        try:
            req = request.Request(
                url,
                data=json.dumps(data).encode('utf-8'),
                headers=headers
            )
            
            with request.urlopen(req, timeout=ConfigConstants.AI_REQUEST_TIMEOUT) as response:
                response_text = response.read().decode('utf-8')
            
            try:
                json_resp = json.loads(response_text)
                if "choices" in json_resp and len(json_resp['choices']) > 0:
                    return json_resp['choices'][0]['message']['content']
            except json.JSONDecodeError:
                logger.error("AI 响应解析失败")
            
            return None
        
        except error.HTTPError as e:
            logger.error(f"HTTP 错误 {e.code}: {e.reason}")
            return None
        except error.URLError as e:
            logger.error(f"网络错误: {e.reason}")
            return None
        except TimeoutError:
            logger.error("请求超时")
            return None
    
    def _build_prompt(self, clean_html: str) -> str:
        """构建 AI 提示词"""
        lines = [
            "You are a web scraping expert. Analyze this AI chat interface HTML to identify critical elements.",
            "",
            "## CRITICAL RULES:",
            "1. **Uniqueness is Key**: Ensure selectors matches ONLY the intended element.",
            "2. **Distinguish AI vs User**: For `result_container`, specificy the selector to target the **AI's response text** only. It MUST exclude user prompts, sidebars, or chat history.",
            "3. **Use Hierarchy**: If a class like `.prose` or `.markdown` is used for both User and AI, you MUST find a unique parent class to differentiate (e.g., `.bot-msg .prose`).",
            "4. **Syntax**: Use standard CSS selectors. Spaces for descendants (e.g., `div.bot p`) are encouraged for precision.",
            "5. **No Invalid Syntax**: Do NOT use `xpath`, `~`, `:has()`, or `text()`.",
            "",
            "## PREFERENCE ORDER:",
            "1. `id`, `name`, `data-testid` (Most preferred)",
            "2. `button[type=\"submit\"]`",
            "3. Unique parent class + target class (e.g., `.response-area .content`)",
            "4. Hashed classes (only if no other option exists)",
            "",
            "## REQUIRED OUTPUT (JSON ONLY):",
            "Return a JSON object with these 6 keys:",
            "- `input_box`: The text input area (textarea/input).",
            "- `send_btn`: The button that sends the message (usually type=\"submit\").",
            "- `result_container`: The container for the AI's generated text response. **(Check parent containers to ensure it excludes user bubbles)**.",
            "- `new_chat_btn`: Button or Link to start a fresh conversation (or null).",
            "- `message_wrapper`: (Optional) The outer container that wraps a complete message turn, including thinking process and response. Usually has `data-turn-role` or similar attribute. Set to null if not identifiable.",
            "- `generating_indicator`: (Optional) Element that indicates AI is still generating (e.g., stop button, loading spinner). Set to null if not identifiable.",
            "- `upload_btn`: (Optional) Button or icon that opens the file picker (paperclip / attachment icon). Set to null if not identifiable.",
"- `file_input`: (Optional) `<input type=\"file\">` element in the DOM (may be hidden). **Check the whole DOM**, hidden input is common. Set to null if not found.",
            "## HTML:",
            clean_html
        ]
        return "\n".join(lines)
    
    def _extract_json(self, text: str) -> Optional[Dict]:
        """从 AI 响应中提取 JSON"""
        try:
            match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
            if match:
                return json.loads(match.group(1))
            
            match = re.search(r'(\{[\s\S]*\})', text)
            if match:
                return json.loads(match.group(1))
            
            return json.loads(text)
        
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}")
            return None


# ================= 配置引擎 =================

class ConfigEngine:
    """配置引擎主类"""
    
    def __init__(self):
        self.config_file = ConfigConstants.CONFIG_FILE
        self.last_mtime = 0.0  # 记录文件最后修改时间
        self.sites: Dict[str, SiteConfig] = self._load_config()
        
        self.html_cleaner = HTMLCleaner()
        self.validator = SelectorValidator()
        self.ai_analyzer = AIAnalyzer()
        
        logger.info(f"配置引擎已初始化，已加载 {len(self.sites)} 个站点配置")
    
    def _load_config(self) -> Dict[str, SiteConfig]:
        """初始化加载配置文件"""
        if not os.path.exists(self.config_file):
            logger.info(f"配置文件 {self.config_file} 不存在，将创建新文件")
            return {}
        
        try:
            # 记录修改时间
            self.last_mtime = os.path.getmtime(self.config_file)
            
            with open(self.config_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {}
                
                data = json.loads(content)
                logger.info(f"已加载配置文件: {self.config_file} (mtime: {self.last_mtime})")
                return data
        
        except json.JSONDecodeError as e:
            logger.error(f"配置文件格式错误: {e}")
            return {}
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
            return {}
    
    def refresh_if_changed(self):
        """
        检查文件是否变化，如果变化则重载
        用于 get_site_config 开头，实现热更新
        """
        if not os.path.exists(self.config_file):
            return

        try:
            current_mtime = os.path.getmtime(self.config_file)
            # 如果修改时间有变化，尝试重载
            if current_mtime != self.last_mtime:
                logger.info(f"⚡ 检测到配置文件变化 (new mtime: {current_mtime})")
                self.reload_config()
        except Exception as e:
            logger.error(f"检查文件变化失败: {e}")

    def reload_config(self):
        """
        重新加载配置（Hot Reload）
        解析失败时保留旧配置，不覆盖
        """
        if not os.path.exists(self.config_file):
            logger.warning("重载失败：配置文件不存在")
            return

        try:
            # 先读取 mtime
            mtime = os.path.getmtime(self.config_file)
            
            with open(self.config_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    data = {}
                else:
                    data = json.loads(content)
            
            # 只有解析成功才更新
            self.sites = data
            self.last_mtime = mtime
            logger.info(f"✅ 配置已热重载 (Sites: {len(self.sites)})")
            
        except json.JSONDecodeError as e:
            logger.error(f"❌ 重载配置失败（JSON格式错误），保留旧配置: {e}")
        except Exception as e:
            logger.error(f"❌ 重载配置失败: {e}")

    def _save_config(self):
        """保存配置文件"""
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.sites, f, indent=2, ensure_ascii=False)
            
            # 写入后更新 last_mtime，防止自己写入触发 refresh_if_changed 的重载
            if os.path.exists(self.config_file):
                self.last_mtime = os.path.getmtime(self.config_file)
            
            logger.info(f"配置已保存: {self.config_file}")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
    
    def get_site_config(self, domain: str, html_content: str) -> Optional[SiteConfig]:
        """
        获取站点配置（缓存 + AI 分析）
        
        Args:
            domain: 域名
            html_content: 页面 HTML
            
        Returns:
            站点配置的快照（副本）
        """
        # 1. 尝试热更新：检查文件变化
        self.refresh_if_changed()

        # 检查缓存
        if domain in self.sites:
            config = self.sites[domain]
            
            # 确保有 workflow
            if "workflow" not in config:
                config["workflow"] = DEFAULT_WORKFLOW
                self.sites[domain] = config
                self._save_config()
            
            logger.debug(f"使用缓存配置: {domain}")
            # 返回深拷贝快照，保证 browser_core 使用期间配置不被外部修改影响
            return copy.deepcopy(config)
        
        # AI 识别
        logger.info(f"🔍 未知域名 {domain}，启动 AI 识别...")
        
        # 清理 HTML
        clean_html = self.html_cleaner.clean(html_content)
        
        # AI 分析
        selectors = self.ai_analyzer.analyze(clean_html)
        
        if selectors:
            # 验证选择器
            selectors = self.validator.validate(selectors)
            
            # 构建配置
            new_config: SiteConfig = {
                "selectors": selectors,
                "workflow": DEFAULT_WORKFLOW,
                "stealth": self._guess_stealth(domain),
                # 可选：流式监控配置（使用默认值）
                "stream_config": {
                    "silence_threshold": 2.5,    # 静默阈值（秒）
                    "initial_wait": 30.0,        # 初始等待时间（秒）
                    "enable_wrapper_search": True  # 是否启用容器向上查找
                }
            }
            
            # 保存
            self.sites[domain] = new_config
            self._save_config()
            
            logger.info(f"✅ 配置已生成并保存: {domain}")
            return copy.deepcopy(new_config)
        
        # 使用回退配置
        logger.warning(f"⚠️  AI 分析失败，使用通用回退配置: {domain}")
        fallback_config: SiteConfig = {
            "selectors": FALLBACK_SELECTORS.copy(),
            "workflow": DEFAULT_WORKFLOW,
            "stealth": False,
            "stream_config": {
                "silence_threshold": 2.5,
                "initial_wait": 30.0,
                "enable_wrapper_search": True
            }
        }
        
        self.sites[domain] = fallback_config
        self._save_config()
        
        return copy.deepcopy(fallback_config)
    
    def _guess_stealth(self, domain: str) -> bool:
        """推测是否需要隐身模式"""
        for stealth_domain in ConfigConstants.STEALTH_DOMAINS:
            if stealth_domain in domain:
                logger.info(f"检测到需要隐身模式的域名: {domain}")
                return True
        return False
    
    def delete_site_config(self, domain: str) -> bool:
        """
        删除指定站点配置
        """
        # 删除前也检查一下是否有新配置，避免覆盖他人更改（可选，视并发需求而定）
        self.refresh_if_changed()
        
        if domain in self.sites:
            del self.sites[domain]
            self._save_config()
            logger.info(f"已删除配置: {domain}")
            return True
        return False


# ================= 单例 =================

config_engine = ConfigEngine()


# ================= 测试入口 =================

if __name__ == "__main__":
    logging.getLogger('config_engine').setLevel(logging.DEBUG)
    
    try:
        from DrissionPage import ChromiumPage
        
        print("连接浏览器...")
        # 仅测试，不实际连接
        print(f"当前配置项数量: {len(config_engine.sites)}")
        
        # 模拟文件更新测试
        print("\n--- 模拟热更新测试 ---")
        original_mtime = config_engine.last_mtime
        print(f"初始 mtime: {original_mtime}")
        
        # 强制保存一次，应该更新 mtime
        config_engine._save_config()
        print(f"保存后 mtime: {config_engine.last_mtime}")
        
        if config_engine.last_mtime != original_mtime:
            print("✅ _save_config 成功更新了 mtime")
        
        # 模拟外部修改
        print("\n--- 模拟外部修改 sites.json ---")
        time.sleep(1.1) # 确保 mtime 变化
        try:
            with open("sites.json", "w", encoding="utf-8") as f:
                json.dump({"test.com": {"selectors": {}, "workflow": []}}, f)
            print("外部文件已写入")
            
            # 调用 refresh_if_changed
            print("调用 refresh_if_changed()...")
            config_engine.refresh_if_changed()
            
            if "test.com" in config_engine.sites:
                print("✅ 热更新成功，检测到 test.com")
            else:
                print("❌ 热更新失败")
                
        except Exception as e:
            print(f"文件操作失败: {e}")
            
    except Exception as e:

        print(f"Error: {e}")
