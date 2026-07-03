"""
bot-hosting.net 自动续期 — SeleniumBase UC 过 Cloudflare Turnstile

代理由 workflow 用 sing-box 起好后监听在 127.0.0.1:8080，本脚本只需要读
环境变量 PROXY = http://127.0.0.1:8080 就会走代理。

环境变量：
  TOKEN          bot-hosting.net localStorage token（必需）
  PROXY          本地代理地址，如 http://127.0.0.1:8080（可选，空则直连）
  TG_BOT_TOKEN   Telegram bot token（可选，配了才发通知）
  TG_CHAT_ID     Telegram chat id（可选）
"""

import os
import sys
import traceback
from pathlib import Path

import requests
from seleniumbase import SB

HERE = Path(__file__).resolve().parent
HOME_URL = "https://bot-hosting.net/"
BILLINGS_URL = "https://bot-hosting.net/a/billings"
RENEW_TEXT = "Renew"
COOLDOWN_BETWEEN_CLICKS = 3


# ============ Telegram ============

def _tg_enabled() -> bool:
    return bool(os.environ.get("TG_BOT_TOKEN") and os.environ.get("TG_CHAT_ID"))


def tg_text(text: str) -> None:
    if not _tg_enabled():
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{os.environ['TG_BOT_TOKEN']}/sendMessage",
            data={
                "chat_id": os.environ["TG_CHAT_ID"],
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=20,
        )
        print(f"[TG] sendMessage {r.status_code}")
    except Exception as e:
        print(f"[TG] sendMessage failed: {e}")


def tg_file(path: str | Path, caption: str = "", kind: str = "document") -> None:
    if not _tg_enabled():
        return
    p = Path(path)
    if not p.is_file():
        return
    method = "sendPhoto" if kind == "photo" else "sendDocument"
    field = "photo" if kind == "photo" else "document"
    try:
        with open(p, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{os.environ['TG_BOT_TOKEN']}/{method}",
                data={"chat_id": os.environ["TG_CHAT_ID"], "caption": caption[:1000]},
                files={field: f},
                timeout=60,
            )
        print(f"[TG] {method} {p.name} {r.status_code}")
    except Exception as e:
        print(f"[TG] {method} failed: {e}")


# ============ 续期主流程 ============

def do_renew(proxy: str | None) -> tuple[bool, int]:
    sb_kwargs = dict(
        uc=True,
        test=False,
        headed=True,
        xvfb=True,
        locale="en",
    )
    if proxy:
        # SeleniumBase 需要不带 scheme 的 host:port
        sb_kwargs["proxy"] = proxy.replace("http://", "").replace("https://", "")
        print(f"→ SeleniumBase 使用代理：{sb_kwargs['proxy']}")
    else:
        print("→ SeleniumBase 直连运行")

    with SB(**sb_kwargs) as sb:
        # 1) 首页过盾
        print("\n【1/5】打开首页，尝试过 Turnstile ...")
        sb.uc_open_with_reconnect(HOME_URL, 4)
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print(f"  ℹ️  uc_gui_click_captcha: {e}")
        sb.sleep(3)

        # 2) 注入 token
        print("\n【2/5】注入 localStorage.token ...")
        token = os.environ["TOKEN"]
        sb.execute_script("localStorage.setItem('token', arguments[0])", token)
        got = sb.execute_script("return localStorage.getItem('token')")
        if got != token:
            raise RuntimeError("localStorage 注入失败")
        print("  ✓ token 已写入")

        # 3) 跳 billings
        print(f"\n【3/5】跳转 {BILLINGS_URL} ...")
        sb.uc_open_with_reconnect(BILLINGS_URL, 4)
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print(f"  ℹ️  uc_gui_click_captcha: {e}")
        sb.sleep(5)
        print(f"  页面标题: {sb.get_title()}")
        print(f"  当前 URL: {sb.get_current_url()}")

        # 4) 找 Renew 按钮
        print(f"\n【4/5】查找 '{RENEW_TEXT}' 按钮 ...")
        xpath = (
            f"//button[normalize-space()='{RENEW_TEXT}']"
            f" | //a[normalize-space()='{RENEW_TEXT}']"
            f" | //button[contains(normalize-space(), '{RENEW_TEXT}')]"
        )
        try:
            sb.wait_for_element(xpath, by="xpath", timeout=20)
        except Exception:
            print(f"  ✗ 20 秒内没找到 '{RENEW_TEXT}' 按钮，dump 页面供排查")
            (HERE / "page_debug.html").write_text(sb.get_page_source(), encoding="utf-8")
            sb.save_screenshot(str(HERE / "page_debug.png"))
            return False, 0

        buttons = sb.find_elements(xpath, by="xpath")
        print(f"  ✓ 找到 {len(buttons)} 个按钮")

        # 5) 逐个点
        print("\n【5/5】逐个点击 Renew 按钮 ...")
        clicked = 0
        for i in range(len(buttons)):
            fresh = sb.find_elements(xpath, by="xpath")
            if i >= len(fresh):
                break
            btn = fresh[i]
            try:
                text = (btn.text or "").strip()
                enabled = btn.is_enabled()
                print(f"\n  [{i+1}/{len(buttons)}] '{text}' enabled={enabled}")
                if not enabled:
                    print("    ⚠️  按钮 disabled，跳过")
                    continue
                btn.click()
                clicked += 1
                sb.sleep(COOLDOWN_BETWEEN_CLICKS)

                # 处理 SweetAlert 确认弹窗
                try:
                    confirm = sb.find_element("button.swal-button--confirm", timeout=3)
                    if confirm and confirm.is_displayed():
                        confirm.click()
                        print("    ✓ 点掉确认弹窗")
                        sb.sleep(2)
                except Exception:
                    pass

                # 处理可能的二次盾
                try:
                    sb.uc_gui_click_captcha()
                except Exception:
                    pass
            except Exception as e:
                print(f"    ✗ 点击失败: {type(e).__name__}: {e}")

        sb.save_screenshot(str(HERE / "after_renew.png"))
        print(f"\n✅ 共点击 {clicked} 个 Renew 按钮")
        return clicked > 0, clicked


# ============ 入口 ============

def main() -> int:
    token = os.environ.get("TOKEN", "")
    if not token:
        print("✗ 缺少 TOKEN")
        tg_text("❌ <b>bot-hosting renew</b>\n缺少 TOKEN 环境变量")
        return 2

    proxy = os.environ.get("PROXY", "").strip() or None

    tg_text(
        f"🚀 <b>bot-hosting renew</b>\n开始运行\n"
        f"代理：{'✓ ' + proxy if proxy else '✗ 直连'}"
    )

    try:
        success, clicked = do_renew(proxy)
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        tg_text(f"❌ <b>bot-hosting renew</b>\n<pre>{str(e)[:1000]}</pre>")
        tg_file(HERE / "page_debug.png", "page_debug", "photo")
        tg_file(HERE / "page_debug.html", "page_debug.html", "document")
        return 1

    if success:
        tg_text(f"✅ <b>bot-hosting renew</b>\n成功，点击了 {clicked} 个按钮")
        tg_file(HERE / "after_renew.png", f"after_renew ({clicked} clicks)", "photo")
        return 0
    else:
        tg_text("❌ <b>bot-hosting renew</b>\n没找到可点的 Renew 按钮")
        tg_file(HERE / "page_debug.png", "page_debug", "photo")
        tg_file(HERE / "page_debug.html", "page_debug.html", "document")
        return 1


if __name__ == "__main__":
    sys.exit(main())
