"""Playwright 浏览器会话：ref 编号元素定位 + 截图

核心思路（browser-use 风格）：
- 每次操作后注入 JS 收集页面可交互元素（a/button/input/select/...），
  打上 data-agent-ref 编号，返回文本化元素清单给模型
- 模型按 ref 编号点击/输入，比坐标或 CSS 选择器稳定得多
- screenshot() 返回 PNG bytes，交给视觉模型分析复杂场景
"""
from __future__ import annotations

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

_COLLECT_JS = """
() => {
  document.querySelectorAll('[data-agent-ref]').forEach(el => el.removeAttribute('data-agent-ref'));
  const sel = 'a, button, input, textarea, select, summary, [role="button"], [role="link"], '
            + '[role="tab"], [role="checkbox"], [role="radio"], [role="option"], [role="menuitem"], '
            + '[contenteditable="true"], [onclick]';
  const els = [...document.querySelectorAll(sel)];
  let i = 0;
  const lines = [];
  for (const el of els) {
    if (i >= 120) { lines.push('...元素过多，仅显示前 120 个'); break; }
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
    if (!el.offsetParent && st.position !== 'fixed') continue;
    const ref = ++i;
    el.setAttribute('data-agent-ref', String(ref));
    const tag = el.tagName.toLowerCase();
    let text = (el.innerText || el.getAttribute('aria-label') || el.value
                || el.placeholder || el.alt || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    let extra = '';
    if (tag === 'input' || tag === 'textarea') {
      extra = ` type=${el.type || 'text'}`;
      if (el.value) extra += ` value="${el.value.slice(0, 50)}"`;
      if (el.placeholder) extra += ` placeholder="${el.placeholder.slice(0, 50)}"`;
      if (el.required) extra += ' required';
    } else if (tag === 'select') {
      const opt = el.selectedOptions && el.selectedOptions[0];
      extra = ` selected="${opt ? opt.text.slice(0, 40) : ''}"`;
    } else if (tag === 'a' && el.href) {
      extra = ` href=${el.href.slice(0, 120)}`;
    } else if (tag === 'button' && el.type === 'submit') {
      extra = ' [submit]';
    }
    const dis = el.disabled ? ' [disabled]' : '';
    lines.push(`[ref=${ref}] <${tag}${extra}> ${text}${dis}`);
  }
  return { url: location.href, title: document.title, elements: lines.join('\\n') };
}
"""


# 默认伪装成真实桌面 Chrome：Playwright 默认 UA 含 "HeadlessChrome"，会被 Bing 等站点
# 判定为机器人从而 302 回首页/返回空页。实测（2026-09-25）：默认 UA 首访
# https://www.bing.com/search?q=... 仅得 42~113 字符空页；改真实 UA 后 3/3 稳定
# 拿到 ~2000 字符结果页。真实 UA 也能降低其它站点风控命中率。
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# 反自动化检测：抹掉 navigator.webdriver 等显式 bot 标记，降低被风控拦截概率。
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


class BrowserSession:
    """同步 Playwright 浏览器会话（单 page）。非线程安全，外部自行串行调用。"""

    def __init__(
        self,
        headless: bool = True,
        timeout: int = 30,
        viewport: list | None = None,
        user_agent: str | None = None,
    ):
        self._timeout_ms = timeout * 1000
        self._viewport = {"width": (viewport or [1280, 800])[0], "height": (viewport or [1280, 800])[1]}
        self._user_agent = user_agent
        self._headless = headless
        self._pw = None
        self._browser = None
        self._context = None
        self.page: Page | None = None
        self._last_snapshot = ""

    # -------- 生命周期 --------

    def ensure_started(self):
        if self.page is not None:
            return
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless)
        self._context = self._browser.new_context(
            viewport=self._viewport,
            user_agent=self._user_agent or _DEFAULT_UA,
            locale="zh-CN",
        )
        try:  # 反检测脚本失败不应阻断启动
            self._context.add_init_script(_STEALTH_JS)
        except Exception:  # noqa: BLE001
            pass
        self._context.set_default_timeout(self._timeout_ms)
        self.page = self._context.new_page()

    def close(self):
        for obj in (self._context, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self.page = None

    # -------- 内部工具 --------

    def _settle(self, timeout: int = 5000):
        """尽力等待网络空闲；动态站点常无法在超时内 idle，此处静默忽略，不阻断操作。"""
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout)
        except PWTimeout:
            pass
        except Exception:  # noqa: BLE001
            pass

    # -------- 新标签页（target="_blank" / window.open）接管 --------

    def _wait_new_page(self, before: list, total_ms: int = 3000, step: int = 200):
        """轮询等待点击/按键后出现的新标签页，返回新 Page；无则返回 None。"""
        elapsed = 0
        while True:
            try:
                pages = list(self._context.pages)
            except Exception:  # noqa: BLE001
                return None
            new = [p for p in pages if p not in before]
            if new:
                return new[-1]
            if elapsed >= total_ms:
                return None
            try:
                self.page.wait_for_timeout(step)
            except Exception:  # noqa: BLE001
                return None
            elapsed += step

    def _adopt_new_page(self, before: list) -> bool:
        """若操作引发新标签页，则把会话切到该页（含跟随其加载）。返回是否发生切换。"""
        np = self._wait_new_page(before)
        if np is None:
            return False
        try:
            np.wait_for_load_state("domcontentloaded", timeout=self._timeout_ms)
        except Exception:  # noqa: BLE001
            pass
        self.page = np
        self._settle()
        return True

    # -------- 快照 --------

    def snapshot(self, take_screenshot: bool = False) -> str:
        """返回当前页面状态文本（URL/标题/可交互元素清单）。"""
        self.ensure_started()
        info = self.page.evaluate(_COLLECT_JS)
        self._last_snapshot = f"当前页面: {info['url']}\n标题: {info['title']}\n\n{info['elements'] or '(无可交互元素)'}"
        return self._last_snapshot

    # -------- 操作 --------

    def navigate(self, url: str) -> str:
        self.ensure_started()
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        except PWTimeout:
            return f"[导航超时] {url} 加载超过 {self._timeout_ms/1000}s，页面可能仍在加载。"
        except Exception as e:  # noqa: BLE001 转换网络错误为可行动的提示
            msg = str(e)
            if "ERR_CONNECTION_TIMED_OUT" in msg or "ERR_CONNECTION_RESET" in msg:
                return (
                    f"[无法访问] {url} 连接超时/被重置。该网站在当前网络环境很可能不可访问"
                    f"（google.com、facebook.com 等被墙站点均无法打开）。"
                    f"请改用国内可直接访问的站点（bing.com、baidu.com 等），或改用 web_search 工具搜索。"
                )
            if "ERR_NAME_NOT_RESOLVED" in msg:
                return f"[域名解析失败] {url} 域名不存在或拼写错误，请检查 URL 后重试。"
            return f"[导航失败] {type(e).__name__}: {msg[:200]}"
        self._settle()
        return self.snapshot()

    def _locate(self, ref: int):
        locator = self.page.locator(f"[data-agent-ref='{ref}']").first
        if locator.count() == 0:
            raise ValueError(
                f"ref={ref} 的元素已失效（页面可能变化），请根据最新 snapshot 重新选择。"
            )
        return locator

    def click(self, ref: int) -> str:
        self.ensure_started()
        before = list(self._context.pages)
        try:
            self._locate(ref).click(timeout=8000)
        except ValueError:
            raise
        except PWTimeout:
            if self._adopt_new_page(before):
                return "[点击打开了新标签页，已切换到新页面]\n" + self.snapshot()
            return "[点击超时] 元素可能触发页面跳转。" + self.snapshot()
        if self._adopt_new_page(before):
            return "[点击打开了新标签页，已切换到新页面]\n" + self.snapshot()
        self._settle()
        return self.snapshot()

    def type(self, ref: int, text: str, clear: bool = True, press_enter: bool = False) -> str:
        self.ensure_started()
        before = list(self._context.pages)
        loc = self._locate(ref)
        loc.click(timeout=8000)
        if clear:
            loc.fill("", timeout=5000)
        loc.type(text, timeout=10000, delay=30)
        if press_enter:
            loc.press("Enter", timeout=5000)
            if self._adopt_new_page(before):
                return "[回车打开了新标签页，已切换到新页面]\n" + self.snapshot()
            self._settle()
            return self.snapshot()
        return self.snapshot()

    def select(self, ref: int, value: str) -> str:
        self.ensure_started()
        loc = self._locate(ref)
        picked = False
        for kwargs in ({"value": value}, {"label": value}):
            try:
                loc.select_option(timeout=3000, **kwargs)
                picked = True
                break
            except Exception:  # noqa: BLE001 尝试下一种匹配方式
                continue
        if not picked:
            opts = []
            try:
                opts = loc.evaluate("el => Array.from(el.options).map(o => o.value + '|' + o.text)")
            except Exception:  # noqa: BLE001
                pass
            raise ValueError(
                f"select 失败：下拉框中不存在 value/label = {value!r} 的选项，可选值={opts}"
            )
        return self.snapshot()

    def press_key(self, key: str) -> str:
        self.ensure_started()
        before = list(self._context.pages)
        self.page.keyboard.press(key)
        if self._adopt_new_page(before):
            return "[按键打开了新标签页，已切换到新页面]\n" + self.snapshot()
        self._settle()
        return self.snapshot()

    def scroll(self, direction: str = "down", amount: int = 3) -> str:
        self.ensure_started()
        delta = 600 * max(1, min(amount, 10))
        if direction.lower() == "up":
            self.page.mouse.wheel(0, -delta)
        else:
            self.page.mouse.wheel(0, delta)
        self.page.wait_for_timeout(500)
        return self.snapshot()

    def screenshot(self) -> tuple[str, bytes]:
        """截图，返回 (描述, png_bytes)。"""
        self.ensure_started()
        png = self.page.screenshot(type="png", full_page=False)
        return self._last_snapshot or self.snapshot(), png

    def extract_text(self, max_chars: int = 6000) -> str:
        self.ensure_started()
        text = self.page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
        text = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n...[截断，共 {len(text)} 字符]"
        return text

    def get_url(self) -> str:
        return self.page.url if self.page else ""
