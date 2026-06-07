import os
import re
import json
import time
import datetime
import statistics

from bs4 import BeautifulSoup
from groq import Groq

try:
    from curl_cffi import requests as cffi
except Exception:
    cffi = None
import requests as plain_requests

TARGETS_FILE = "targets.json"
PRICES_FILE = "prices.json"
MODEL = "llama-3.3-70b-versatile"

FETCH_TIMEOUT = 25
TEXT_LIMIT = 4000
GROQ_PAUSE = 3
DROP_ALERT_PERCENT = 20

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

client = Groq(api_key=GROQ_API_KEY)

EXTRACT_SYSTEM = (
    "أنت محلل بيانات آلي. استخرج السعر النهائي الكاش بالريال السعودي من هذا النص. "
    "تجاهل تماماً أي أسعار مقترنة بكلمات (تابي، تمارا، تقسيط، دفعات، شهرياً، مقدم، Tabby، Tamara). "
    "يجب أن تكون إجابتك حصراً بصيغة JSON صحيحة بهذا الهيكل: "
    '{"price": float, "status": "available_or_out_of_stock"}. '
    "إن لم تجد سعراً كاش واضحاً اجعل price=null. لا تكتب أي نص إضافي."
)


def load_targets():
    try:
        if os.path.exists(TARGETS_FILE):
            with open(TARGETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return [t for t in data if t.get("url") and t.get("name")]
    except Exception as e:
        print(f"تحذير: تعذّر قراءة {TARGETS_FILE} ({e})")
    return []


def load_prices():
    try:
        if os.path.exists(PRICES_FILE):
            with open(PRICES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"تحذير: تعذّر قراءة {PRICES_FILE} ({e})")
    return {}


def save_prices(data):
    try:
        with open(PRICES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"تحذير: تعذّر حفظ {PRICES_FILE} ({e})")


def fetch_html(url):
    """جلب مباشر سريع متجاوزاً Cloudflare عبر curl_cffi (مع بديل احتياطي)."""
    if cffi is not None:
        try:
            r = cffi.get(url, impersonate="chrome", timeout=FETCH_TIMEOUT)
            if r.status_code == 200 and r.text:
                return r.text
            print(f"   كود الحالة {r.status_code}")
        except Exception as e:
            print(f"   تخطٍّ الجلب ({type(e).__name__})")
    # بديل احتياطي بسيط
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
        r = plain_requests.get(url, headers=headers, timeout=FETCH_TIMEOUT)
        if r.status_code == 200:
            return r.text
        print(f"   (بديل) كود الحالة {r.status_code}")
    except Exception as e:
        print(f"   (بديل) فشل الجلب ({type(e).__name__})")
    return None


def clean_text(html):
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "head"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True))
        return text[:TEXT_LIMIT]
    except Exception as e:
        print(f"   تعذّر تنظيف HTML ({type(e).__name__})")
        return None


def num(x):
    try:
        v = float(re.sub(r"[^\d.]", "", str(x).replace(",", "")))
        return v if v > 0 else None
    except Exception:
        return None


def extract_price(text):
    try:
        c = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=120,
            response_format={"type": "json_object"},
        )
        d = json.loads(c.choices[0].message.content)
        return num(d.get("price")), str(d.get("status", "")).strip()
    except Exception as e:
        msg = str(e).lower()
        if "rate" in msg or "429" in msg:
            print("   Groq مزدحم، انتظار 25ث وإعادة...")
            time.sleep(25)
            try:
                c = client.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "system", "content": EXTRACT_SYSTEM},
                              {"role": "user", "content": text}],
                    temperature=0, max_tokens=120,
                    response_format={"type": "json_object"},
                )
                d = json.loads(c.choices[0].message.content)
                return num(d.get("price")), str(d.get("status", "")).strip()
            except Exception as e2:
                print(f"   فشل الاستخراج بعد الإعادة ({type(e2).__name__})")
        else:
            print(f"   خطأ Groq ({type(e).__name__})")
    return None, ""


def send_telegram(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("   (تنبيه تليجرام غير مُفعّل)")
        return
    try:
        api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = plain_requests.post(api, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)
        print("   ✅ أُرسل تنبيه تليجرام" if r.status_code == 200 else f"   تحذير: تليجرام {r.status_code}")
    except Exception as e:
        print(f"   تحذير: فشل تليجرام ({e})")


def main():
    print("بدء جولة V4.0 (Direct Hybrid Targeter)...")
    targets = load_targets()
    prices = load_prices()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")

    for t in targets:
        name, url = t["name"], t["url"]
        print(f"\nالمنتج: {name}")

        if name not in prices or not isinstance(prices[name], dict):
            prices[name] = {"url": url, "readings": []}
        prices[name]["url"] = url
        prices[name].setdefault("readings", [])
        prices[name]["last_checked"] = now

        old = [r["price"] for r in prices[name]["readings"] if r.get("price")]
        hist_low = min(old) if old else None

        html = fetch_html(url)
        if not html:
            prices[name]["last_status"] = "تعذّر جلب الصفحة"
            continue

        text = clean_text(html)
        if not text:
            prices[name]["last_status"] = "تعذّر قراءة المحتوى"
            continue

        price, status = extract_price(text)
        time.sleep(GROQ_PAUSE)

        if not price:
            prices[name]["last_status"] = "لم يُعثر على سعر"
            print("   لا سعر.")
            continue

        prices[name]["readings"].append({"t": now, "price": price, "status": status, "url": url})
        prices[name]["readings"] = prices[name]["readings"][-200:]
        prices[name]["last_status"] = status or "available"
        print(f"   السعر: {price} SAR | {status}")

        if hist_low and price < hist_low:
            drop = round((hist_low - price) / hist_low * 100, 1)
            if drop >= DROP_ALERT_PERCENT:
                send_telegram(
                    f"🚨 انهيار في السعر! منتج {name} نزل بنسبة {drop}%. "
                    f"السعر الآن {price} ريال. رابط الشراء: {url}"
                )
                print(f"   🚨 انخفاض {drop}% — أُطلق التنبيه")

    save_prices(prices)
    print("\nتم الحفظ في prices.json")


if __name__ == "__main__":
    main()
