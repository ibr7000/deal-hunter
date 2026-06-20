import os
import json
import time
import datetime

import requests
from groq import Groq

TARGETS_FILE = "targets.json"
PRICES_FILE = "prices.json"
MODEL = "llama-3.3-70b-versatile"
SERPER_URL = "https://google.serper.dev/shopping"
RESULTS_PER_PRODUCT = 5
GROQ_PAUSE = 2
DROP_ALERT_PERCENT = 20

SERPER_API_KEY = os.environ.get("SERPER_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

print(f"🔑 Serper: {bool(SERPER_API_KEY)} | Groq: {bool(GROQ_API_KEY)}")
client = Groq(api_key=GROQ_API_KEY)

EXTRACT_SYSTEM = (
    "أنت محلل صفقات آلي محترف وصارم جداً. ستُعطى اسم منتج وقائمة نتائج تسوّق (عنوان + سعر + متجر + رابط). "
    "مهمتك اختيار أرخص سعر منطقي للمنتج الأساسي الكامل فقط.\n"
    "قواعد إلزامية صارمة:\n"
    "1) استبعد تماماً أي نتيجة تكون ملحقاً أو إكسسواراً أو قطعة غيار، ومن ذلك (على سبيل المثال لا الحصر): "
    "كفر، جراب، غلاف، حافظة، شاحن، كيبل، سلك، محول، حماية شاشة، لاصقة، ستاند، حامل، سماعة منفصلة، "
    "بطارية، قطع غيار، case, cover, charger, cable, adapter, screen protector, stand, holder, strap, "
    "band, accessory, spare part.\n"
    "2) العنوان يجب أن يطابق المنتج الأساسي المطلوب نفسه (نفس الجهاز/المنتج)، لا منتجاً مختلفاً ولا نسخة مصغّرة ولا ملحقاً له.\n"
    "3) تجاهل أسعار التقسيط (تابي، تمارا، تقسيط، دفعات، شهرياً، مقدم، Tabby، Tamara). المطلوب السعر الكاش الكامل.\n"
    "4) السعر يجب أن يكون لوحدة المنتج الأساسي كاملة، لا لقطعة أو ملحق أو عرض جزئي.\n"
    "5) ⚠️ مهم جداً: إذا كانت كل النتائج المتاحة مجرد إكسسوارات أو ملحقات أو قطع غيار، أو لا يوجد بينها المنتج الأساسي، "
    "فلا تختر أي سعر، واجعل price=null.\n"
    "6) لا تختر سعراً لمجرد أنه الأرخص؛ تأكد أولاً أنه للمنتج الأساسي الكامل. السعر المنخفض جداً مقارنةً بالبقية غالباً إكسسوار، فاستبعده.\n"
    "أعد إجابتك حصراً بصيغة JSON صحيحة بهذا الهيكل فقط دون أي نص إضافي: "
    '{"price": float, "store_name": "string", "url": "string"}. '
    "عند عدم وجود منتج أساسي مناسب اجعل price=null وstore_name و url فارغين."
)


def load_targets():
    try:
        if not os.path.exists(TARGETS_FILE):
            print("⚠️ targets.json غير موجود!")
            return []
        with open(TARGETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        valid = [t for t in data if isinstance(t, dict) and t.get("name")]
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


def serper_shopping(name):
    """يبحث في Google Shopping عبر Serper ويرجع أول 5 نتائج."""
    if not SERPER_API_KEY:
        print("   ⛔ مفتاح Serper غير موجود.")
        return []
    try:
        headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
        payload = {"q": name, "gl": "sa", "hl": "ar"}
        r = requests.post(SERPER_URL, headers=headers, json=payload, timeout=25)
        if r.status_code != 200:
            print(f"   ⛔ Serper كود {r.status_code}")
            return []
        items = r.json().get("shopping", []) or r.json().get("shopping_results", [])
        results = []
        for it in items[:RESULTS_PER_PRODUCT]:
            results.append({
                "title": it.get("title", ""),
                "price": it.get("price", ""),
                "link": it.get("link", ""),
                "source": it.get("source", ""),
            })
        print(f"   🛒 نتائج Serper: {len(results)}")
        return results
    except Exception as e:
        print(f"   ⛔ فشل Serper ({type(e).__name__})")
        return []


def num(x):
    import re
    try:
        v = float(re.sub(r"[^\d.]", "", str(x).replace(",", "")))
        return v if v > 0 else None
    except Exception:
        return None


def decide_best(name, results):
    """يرسل النتائج الـ5 إلى Groq لاختيار أرخص سعر منطقي للمنتج الأساسي."""
    try:
        c = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": f"المنتج المطلوب: {name}\n\nقائمة النتائج:\n{json.dumps(results, ensure_ascii=False)}"},
            ],
            temperature=0,
            max_tokens=160,
            response_format={"type": "json_object"},
        )
        d = json.loads(c.choices[0].message.content)
        price = num(d.get("price"))
        if price:
            return {
                "price": price,
                "store_name": str(d.get("store_name", "")).strip() or "متجر",
                "url": str(d.get("url", "")).strip(),
            }
    except Exception as e:
        msg = str(e).lower()
        if "rate" in msg or "429" in msg:
            print("   Groq مزدحم، انتظار 25ث وإعادة...")
            time.sleep(25)
            try:
                c = client.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "system", "content": EXTRACT_SYSTEM},
                              {"role": "user", "content": f"المنتج المطلوب: {name}\n\nقائمة النتائج:\n{json.dumps(results, ensure_ascii=False)}"}],
                    temperature=0, max_tokens=160,
                    response_format={"type": "json_object"},
                )
                d = json.loads(c.choices[0].message.content)
                price = num(d.get("price"))
                if price:
                    return {"price": price,
                            "store_name": str(d.get("store_name", "")).strip() or "متجر",
                            "url": str(d.get("url", "")).strip()}
            except Exception as e2:
                print(f"   فشل بعد الإعادة ({type(e2).__name__})")
        else:
            print(f"   ⛔ خطأ Groq ({type(e).__name__})")
    return None


def send_telegram(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("   (تنبيه تليجرام غير مُفعّل)")
        return
    try:
        api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(api, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)
        print("   ✅ أُرسل تنبيه تليجرام" if r.status_code == 200 else f"   تحذير: تليجرام {r.status_code}")
    except Exception as e:
        print(f"   تحذير: فشل تليجرام ({e})")


def main():
    print("بدء جولة V5.0 (Autonomous API Hunter)...")
    targets = load_targets()
    if not targets:
        print("⛔ لا توجد أهداف.")
        return

    prices = load_prices()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")

    for t in targets:
        name = t["name"]
        print(f"\nالمنتج: {name}")

        if name not in prices or not isinstance(prices[name], dict):
            prices[name] = {"readings": []}
        prices[name].setdefault("readings", [])
        prices[name]["last_checked"] = now

        old = [r["price"] for r in prices[name]["readings"] if r.get("price")]
        hist_low = min(old) if old else None

        results = serper_shopping(name)
        if not results:
            prices[name]["last_status"] = "لا نتائج بحث"
            continue
        time.sleep(1)

        best = decide_best(name, results)
        time.sleep(GROQ_PAUSE)

        if not best:
            prices[name]["last_status"] = "لم يُعثر على سعر منطقي"
            print("   ⛔ لا سعر منطقي.")
            continue

        prices[name]["readings"].append({
            "t": now, "price": best["price"],
            "store": best["store_name"], "url": best["url"],
        })
        prices[name]["readings"] = prices[name]["readings"][-200:]
        prices[name]["last_status"] = "تم التحديث"
        print(f"   ✅ {best['price']} SAR في {best['store_name']}")

        if hist_low and best["price"] < hist_low:
            drop = round((hist_low - best["price"]) / hist_low * 100, 1)
            if drop >= DROP_ALERT_PERCENT:
                send_telegram(
                    f"🚨 انهيار في السعر! منتج {name} نزل بنسبة {drop}%. "
                    f"السعر الآن {best['price']} ريال في {best['store_name']}. "
                    f"رابط الشراء: {best['url']}"
                )
                print(f"   🚨 انخفاض {drop}%")

    save_prices(prices)
    print("\nتم الحفظ في prices.json")


if __name__ == "__main__":
    main()
