import os
import json
import base64
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="صياد العروض", page_icon="🛒", layout="wide")
st.markdown("<style>.stApp{direction:rtl;text-align:right;}</style>", unsafe_allow_html=True)

WATCHLIST_FILE = "watchlist.txt"
PRICES_FILE = "prices.json"


def secret(key, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return os.environ.get(key, default)


GITHUB_TOKEN = secret("GITHUB_TOKEN")
GITHUB_REPO = secret("GITHUB_REPO")
GITHUB_BRANCH = secret("GITHUB_BRANCH", "main")


def read_watchlist():
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""


def save_watchlist_to_github(text):
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return False, "لم يتم ضبط GITHUB_TOKEN أو GITHUB_REPO."
    try:
        api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{WATCHLIST_FILE}"
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
        sha = None
        r = requests.get(api, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=20)
        if r.status_code == 200:
            sha = r.json().get("sha")
        payload = {
            "message": "تحديث قائمة المراقبة من التطبيق",
            "content": base64.b64encode(text.encode("utf-8")).decode("utf-8"),
            "branch": GITHUB_BRANCH,
        }
        if sha:
            payload["sha"] = sha
        put = requests.put(api, headers=headers, json=payload, timeout=20)
        if put.status_code in (200, 201):
            return True, "تم الحفظ. سيُفحص في الجولة القادمة."
        return False, f"فشل الحفظ: {put.status_code}"
    except Exception as e:
        return False, f"خطأ: {e}"


def read_prices():
    try:
        if os.path.exists(PRICES_FILE):
            with open(PRICES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


st.title("🛒 صياد العروض الذكي — V3")
prices = read_prices()

tab1, tab2 = st.tabs(["🔥 لوحة الأسعار", "📋 المنتجات"])

with tab1:
    rows, chart_rows, waiting = [], [], []

    for name, info in prices.items():
        readings = info.get("readings", []) if isinstance(info, dict) else []
        if not readings:
            waiting.append((name, info.get("last_status", "بانتظار أول سعر")))
            continue

        first, last = readings[0], readings[-1]
        hist = [r["price"] for r in readings if r.get("price")]
        low = min(hist) if hist else None

        # التغير مقابل أول قراءة
        delta = None
        try:
            if first.get("price"):
                delta = round((last["price"] - first["price"]) / first["price"] * 100, 1)
        except Exception:
            delta = None

        # هل السعر الحالي هو الأدنى تاريخياً؟ (فرصة)
        is_best = (low is not None and last.get("price") is not None and last["price"] <= low)

        rows.append({
            "🔥": "🔥" if is_best else "",
            "المنتج": name,
            "أحدث سعر": last.get("price"),
            "أقل سعر مسجل": low,
            "خصم الصفحة %": last.get("discount_pct"),
            "المتجر": last.get("store", ""),
            "التغير الكلي %": delta,
            "الشراء": last.get("url", ""),
        })
        for r in readings:
            if r.get("price"):
                chart_rows.append({"الوقت": r.get("t"), "السعر": r["price"], "المنتج": name})

    if rows:
        df = pd.DataFrame(rows)
        st.subheader("جدول الأسعار  ( 🔥 = الأدنى تاريخياً )")
        st.dataframe(
            df, use_container_width=True, hide_index=True,
            column_config={
                "الشراء": st.column_config.LinkColumn("الشراء", display_text="فتح"),
                "التغير الكلي %": st.column_config.NumberColumn("التغير الكلي %", format="%.1f%%"),
                "خصم الصفحة %": st.column_config.NumberColumn("خصم الصفحة %", format="%.0f%%"),
                "أحدث سعر": st.column_config.NumberColumn("أحدث سعر", format="%.0f"),
                "أقل سعر مسجل": st.column_config.NumberColumn("أقل سعر مسجل", format="%.0f"),
            },
        )
        st.subheader("📈 تغير الأسعار عبر الزمن")
        cdf = pd.DataFrame(chart_rows)
        if not cdf.empty:
            pivot = cdf.pivot_table(index="الوقت", columns="المنتج", values="السعر", aggfunc="last")
            st.line_chart(pivot, height=380)
    else:
        st.info("لا توجد أسعار بعد. شغّل الوكيل من Actions في GitHub ثم عُد.")

    if waiting:
        st.subheader("⏳ منتجات بلا سعر بعد")
        st.caption("لم يُعثر لها على سعر موثوق في آخر جولة (قد تظهر في جولة قادمة).")
        for nm, status in waiting:
            st.markdown(f"- **{nm}** — {status}")

with tab2:
    st.write("اكتب اسم منتج واحد في كل سطر (كن دقيقاً قدر الإمكان):")
    text = st.text_area("قائمتك:", value=read_watchlist(), height=240,
                        placeholder="مطحنة قهوة Baratza Encore\niPhone 16 Pro Max 256GB")
    if st.button("💾 حفظ القائمة", use_container_width=True):
        ok, msg = save_watchlist_to_github(text)
        (st.success if ok else st.error)(msg)
        if not GITHUB_TOKEN:
            st.info("بدون توكن GitHub عدّل ملف watchlist.txt مباشرة من المستودع.")
