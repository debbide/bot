"""
bot-hosting.net 自动续期
- SeleniumBase UC 模式过 Cloudflare Turnstile
- 通过 localStorage.token 登录（token 走环境变量 TOKEN）
- 可选代理：环境变量 PROXY，格式 http://user:pass@host:port 或 socks5://...

在 GitHub Actions 上跑：xvfb=True 需要 runner 已安装 xvfb。
"""

import json
import os
import sys
import time
from seleniumbase import SB

HOME_URL = "https://bot-hosting.net/"
BILLINGS_URL = "https://bot-hosting.net/a/billings"
RENEW_TEXT = "Renew"          # 按钮文字，可改
COOLDOWN_BETWEEN_CLICKS = 3   # 秒


def parse_proxy_for_sb(proxy_url: str | None):
    """SeleniumBase 接受 host:port 或 user:pass@host:port，不带 scheme"""
    if not proxy_url:
        return None
    p = proxy_url.strip()
    for scheme in ("http://", "https://", "socks5://", "socks4://"):
        if p.startswith(scheme):
            p = p[len(scheme):]
            break
    return p.rstrip("/") or None


def main() -> int:
    token = os.getenv("TOKEN")
    if not token:
        print("✗ 缺少环境变量 TOKEN")
        return 2

    proxy = parse_proxy_for_sb(os.getenv("PROXY"))
    if proxy:
        print(f"✓ 使用代理: {proxy}")
    else:
        print("ℹ️  未使用代理（GitHub Actions IP 可能被 CF 标记，注意）")

    sb_kwargs = dict(
        uc=True,
        test=False,
        headed=True,   # UC 模式必须有 display
        xvfb=True,     # 在无头环境提供虚拟显示
        locale="en",
        incognito=False,
    )
    if proxy:
        sb_kwargs["proxy"] = proxy

    with SB(**sb_kwargs) as sb:
        # 1) 打开首页，过盾
        print("\n→ 打开首页，尝试过 Turnstile...")
        sb.uc_open_with_reconnect(HOME_URL, 4)
        try:
            sb.uc_gui_click_captcha()   # 主动点 Turnstile 复选框
        except Exception as e:
            print(f"  ℹ️  uc_gui_click_captcha: {e}（可能已过盾）")
        sb.sleep(3)

        # 2) 注入 localStorage.token
        print("→ 注入 localStorage.token")
        sb.execute_script(
            "localStorage.setItem('token', arguments[0])",
            token,
        )
        # 校验
        got = sb.execute_script("return localStorage.getItem('token')")
        if got != token:
            print("✗ localStorage 注入失败")
            return 3
        print("  ✓ token 已写入")

        # 3) 跳到 billings 页面
        print(f"\n→ 跳转 {BILLINGS_URL}")
        sb.uc_open_with_reconnect(BILLINGS_URL, 4)
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print(f"  ℹ️  uc_gui_click_captcha: {e}")
        sb.sleep(5)

        # 打印页面标题，方便日志排查
        print(f"  页面标题: {sb.get_title()}")
        print(f"  当前 URL: {sb.get_current_url()}")

        # 4) 查找所有 Renew 按钮
        print(f"\n→ 查找按钮文字包含 '{RENEW_TEXT}' 的元素")
        # 用 XPath 匹配 button 或 a 里的 Renew 文本
        xpath = (
            f"//button[normalize-space()='{RENEW_TEXT}']"
            f" | //a[normalize-space()='{RENEW_TEXT}']"
            f" | //button[contains(normalize-space(), '{RENEW_TEXT}')]"
        )
        try:
            sb.wait_for_element(xpath, by="xpath", timeout=20)
        except Exception:
            print(f"  ✗ 20 秒内没找到 '{RENEW_TEXT}' 按钮")
            print("  → 保存页面 HTML 到 page_debug.html 供排查")
            html = sb.get_page_source()
            with open("page_debug.html", "w", encoding="utf-8") as f:
                f.write(html)
            sb.save_screenshot("page_debug.png")
            return 4

        buttons = sb.find_elements(xpath, by="xpath")
        print(f"  ✓ 找到 {len(buttons)} 个按钮")

        # 5) 逐个点击
        clicked = 0
        for i in range(len(buttons)):
            # 每次重新拿元素，避免 DOM 刷新导致 stale
            fresh = sb.find_elements(xpath, by="xpath")
            if i >= len(fresh):
                break
            btn = fresh[i]
            try:
                text = (btn.text or "").strip()
                enabled = btn.is_enabled()
                print(f"\n  [{i+1}/{len(buttons)}] '{text}' enabled={enabled}")
                if not enabled:
                    print("    ⚠️  按钮不可用，跳过")
                    continue
                btn.click()
                clicked += 1
                sb.sleep(COOLDOWN_BETWEEN_CLICKS)

                # 处理可能出现的确认弹窗（SweetAlert）
                try:
                    confirm = sb.find_element(
                        "button.swal-button--confirm", timeout=3
                    )
                    if confirm and confirm.is_displayed():
                        confirm.click()
                        print("    ✓ 已点击确认弹窗 OK")
                        sb.sleep(2)
                except Exception:
                    pass

                # 处理可能的二次 Turnstile
                try:
                    sb.uc_gui_click_captcha()
                except Exception:
                    pass

            except Exception as e:
                print(f"    ✗ 点击失败: {type(e).__name__}: {e}")

        print(f"\n✅ 完成，共点击 {clicked} 个 Renew 按钮")

        # 留个截图存档
        sb.save_screenshot("after_renew.png")
        return 0 if clicked > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
