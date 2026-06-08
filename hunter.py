import os
import re
import json
import time
import datetime

from bs4 import BeautifulSoup
from groq import Groq

CFFI_OK = False
try:
    from curl_cffi import requests as cffi
    CFFI_OK = True
    print("✅ curl_cffi جاهز")
except Exception as e:
    print(f"⚠️ curl_cffi غير متاح ({e})")

import requests as plain_requests

TARGETS_FILE = "targets.json"
PRICES_FILE = "prices.json"
MODEL = "llama-3.3-70b-versatile"
FETCH_TIMEOUT = 25
TEXT_LIMIT = 3000           # حد أقصى للنص المُجمَّع المُرسل لـ Groq
GROQ_PAUSE = 3
DROP_ALERT_PERCENT = 20

# كلمات تدل على وجود سعر — نبحث عنها في كامل الصفحة
PRICE_HINTS = ("sar", "ر.س", "ريال", "price", "السعر", "ر,س", "﷼")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

print(f"🔑 مفتاح Groq موجود: {bool(GROQ_API_KEY)}")
client = Groq(api_key=GROQ_API_KEY)

EXTRACT_SYSTEM = (
    "أنت محلل بيانات آلي. استخرج السعر النهائي الكاش بالريال السعودي من هذا النص. "
    "تجاهل تماماً أي أسعار مقترنة بكلمات (تابي، تمارا، تقسيط، دفعات، شهرياً، مقدم، Tabby، Tamara). "
    "إن وُجد سعر مشطوب قبل الخصم وسعر حالي، فالسعر المطلوب هو الحالي (الأقل). "
    'يجب أن تكون إجابتك حصراً JSON صحيحاً بهذا الهيكل: {"price": float, "status": "available"}. '
    "إن لم تجد سعراً كاش واضحاً اجعل price=null. لا تكتب أي نص إضافي."
)


def load_targets():
    try:
        if not os.path.exists(TARGETS_FILE):
            print("⚠️ targets.json غير موجود!")
            return []
        with open(TARGETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        valid = [t for t in data if t.get("url") and t.get("name")]
        print(f"📋 عدد الأهداف: {len(valid)}")
        return valid
    except Exception as e:
        print(f"⚠️ تعذّر قراءة targets.json ({e})")
    return []


def load_prices():
    try:
        if os.path.exists(PRICES_FILE):
            with open(PRICES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"تحذير: قراءة prices.json ({e})")
    return {}


def save_prices(data):
    try:
        with open(PRICES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"تحذير: حفظ prices.json ({e})")


def fetch_html(url):
    if CFFI_OK:
        try:
            r = cffi.get(url, impersonate="chrome", timeout=FETCH_TIMEOUT)
            print(f"   [curl_cffi] كود {r.status_code} | طول {len(r.text or '')}")
            if r.status_code == 200 and r.text:
                return r.text
        except Exception as e:
            print(f"   [curl_cffi] فشل ({type(e).__name__})")
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                   "Accept-Language": "ar,en;q=0.8"}
        r = plain_requests.get(url, headers=headers, timeout=FETCH_TIMEOUT)
        print(f"   [requests] كود {r.status_code} | طول {len(r.text or '')}")
        if r.status_code == 200:
            return r.text
    except Exception as e:
        print(f"   [requests] فشل ({type(e).__name__})")
    return None


def smart_price_text(html):
    """
    بدل أول 4000 حرف عشوائياً: يبحث في كامل الصفحة عن المقاطع التي تذكر السعر،
    ويجمع كل مقطع مع النص المحيط به، فيضمن وصول السعر مهما كان موقعه.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "head"]):
            tag.decompose()

        full = re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True))
        if not full:
            return None

        low = full.lower()
        chunks, used = [], []

        # نلتقط كل موضع تظهر فيه كلمة سعر، ونأخذ ما حولها (نافذة 220 حرفاً)
        for hint in PRICE_HINTS:
            start = 0
            while True:
                idx = low.find(hint, start)
                if idx == -1:
                    break
                a = max(0, idx - 120)
                b = min(len(full), idx + 100)
                # نتفادى تكرار نفس المنطقة
                if not any(abs(idx - u) < 60 for u in used):
                    chunks.append(full[a:b])
                    used.append(idx)
                start = idx + len(hint)
                if len(chunks) >= 25:
                    break

        if chunks:
            combined = " … ".join(chunks)
            # نضيف بداية الصفحة (غالباً فيها العنوان والسعر الرئيسي) لمزيد من السياق
            combined = full[:600] + " … " + combined
            return combined[:TEXT_LIMIT]

        # إن لم نجد أي كلمة سعر، نرجع لبداية الصفحة كحل أخير
        return full[:TEXT_LIMIT]
    except Exception as e:
        print(f"   تعذّر تجهيز النص ({type(e).__name__})")
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
            messages=[{"role": "system", "content": EXTRACT_SYSTEM},
                      {"role": "user", "content": text}],
            temperature=0, max_tokens=120,
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
                print(f"   فشل بعد الإعادة ({type(e2).__name__})")
        else:
            print(f"   ⛔ خطأ Groq ({type(e).__name__})")
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
    print("بدء جولة V4.1 (استهداف ذكي للسعر)...")
    targets = load_targets()
    if not targets:
        print("⛔ لا توجد أهداف.")
        return

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
            print("   ⛔ تعذّر جلب الصفحة.")
            continue

        text = smart_price_text(html)
        if not text:
            prices[name]["last_status"] = "تعذّر قراءة المحتوى"
            continue
        print(f"   📄 طول النص المُجمَّع: {len(text)}")

        price, status = extract_price(text)
        time.sleep(GROQ_PAUSE)

        if not price:
            prices[name]["last_status"] = "لم يُعثر على سعر"
            print("   ⛔ لا سعر.")
            continue

        prices[name]["readings"].append({"t": now, "price": price, "status": status, "url": url})
        prices[name]["readings"] = prices[name]["readings"][-200:]
        prices[name]["last_status"] = status or "available"
        print(f"   ✅ السعر: {price} SAR | {status}")

        if hist_low and price < hist_low:
            drop = round((hist_low - price) / hist_low * 100, 1)
            if drop >= DROP_ALERT_PERCENT:
                send_telegram(f"🚨 انهيار في السعر! منتج {name} نزل بنسبة {drop}%. "
                              f"السعر الآن {price} ريال. رابط الشراء: {url}")
                print(f"   🚨 انخفاض {drop}%")

    save_prices(prices)
    print("\nتم الحفظ في prices.json")


if __name__ == "__main__":
    main()
