import os
import json
import base64

import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="صياد العروض V4", page_icon="🎯", layout="wide")
st.markdown("<style>.stApp{direction:rtl;text-align:right;}</style>", unsafe_allow_html=True)

TARGETS_FILE = "targets.json"
PRICES_FILE = "prices.json"


def secret(key, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return os.environ.get(key, default)


GITHUB_TOKEN = secret("GITHUB_TOKEN")
GITHUB_REPO = secret("GITHUB_REPO")
GITHUB_BRANCH = secret("GITHUB_BRANCH", "main")


def read_targets():
    """يقرأ قائمة الأهداف من الملف ويعيدها كقائمة بايثون."""
    try:
        if os.path.exists(TARGETS_FILE):
            with open(TARGETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [t for t in data if isinstance(t, dict) and t.get("name") and t.get("url")]
    except Exception:
        pass
    return []


def save_targets_to_github(targets_list):
    """يحوّل القائمة إلى JSON ويحفظها في GitHub. المستخدم لا يرى أي JSON."""
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return False, "لم يتم ضبط GITHUB_TOKEN أو GITHUB_REPO في إعدادات التطبيق."
    try:
        text = json.dumps(targets_list, ensure_ascii=False, indent=2)
        api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{TARGETS_FILE}"
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
        sha = None
        r = requests.get(api, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=20)
        if r.status_code == 200:
            sha = r.json().get("sha")
        payload = {
            "message": "تحديث قائمة الأهداف من التطبيق",
            "content": base64.b64encode(text.encode("utf-8")).decode("utf-8"),
            "branch": GITHUB_BRANCH,
        }
        if sha:
            payload["sha"] = sha
        put = requests.put(api, headers=headers, json=payload, timeout=20)
        if put.status_code in (200, 201):
            return True, "تم الحفظ بنجاح ✅"
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


st.title("🎯 صياد العروض — V4")

# نحتفظ بالقائمة في ذاكرة الجلسة، ونحمّلها أول مرة من الملف
if "targets" not in st.session_state:
    st.session_state.targets = read_targets()

prices = read_prices()
tab1, tab2 = st.tabs(["🔥 لوحة الأسعار", "🎯 منتجاتي"])

# ============ تبويب الأسعار ============
with tab1:
    rows, chart_rows = [], []
    for name, info in prices.items():
        readings = info.get("readings", []) if isinstance(info, dict) else []
        url = info.get("url", "") if isinstance(info, dict) else ""
        if not readings:
            rows.append({"🔥": "", "المنتج": name, "أحدث سعر": None, "أقل سعر مسجل": None,
                         "الحالة": info.get("last_status", "بانتظار سعر"), "الرابط": url})
            continue
        last = readings[-1]
        hist = [r["price"] for r in readings if r.get("price")]
        low = min(hist) if hist else None
        is_best = (low is not None and last.get("price") is not None and last["price"] <= low)
        rows.append({
            "🔥": "🔥" if is_best else "",
            "المنتج": name,
            "أحدث سعر": last.get("price"),
            "أقل سعر مسجل": low,
            "الحالة": last.get("status") or info.get("last_status", ""),
            "الرابط": url,
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
                "الرابط": st.column_config.LinkColumn("الشراء", display_text="فتح"),
                "أحدث سعر": st.column_config.NumberColumn("أحدث سعر", format="%.0f"),
                "أقل سعر مسجل": st.column_config.NumberColumn("أقل سعر مسجل", format="%.0f"),
            },
        )
        st.subheader("📈 تاريخ السعر")
        cdf = pd.DataFrame(chart_rows)
        if not cdf.empty:
            pivot = cdf.pivot_table(index="الوقت", columns="المنتج", values="السعر", aggfunc="last")
            st.line_chart(pivot, height=380)
    else:
        st.info("لا توجد بيانات بعد. أضف منتجاتك من تبويب «منتجاتي» ثم شغّل الوكيل من GitHub.")

# ============ تبويب منتجاتي (بدون أي JSON) ============
with tab2:
    st.subheader("➕ إضافة منتج جديد")
    st.caption("اكتب الاسم والرابط فقط. لا حاجة لأي رموز أو أقواس.")

    new_name = st.text_input("اسم المنتج", placeholder="مثال: ماك بوك برو M4 - جرير")
    new_url = st.text_input("رابط صفحة المنتج", placeholder="https://...")

    if st.button("➕ إضافة المنتج", use_container_width=True):
        nm = (new_name or "").strip()
        ur = (new_url or "").strip()
        if not nm or not ur:
            st.error("الرجاء تعبئة الاسم والرابط معاً.")
        elif not ur.startswith("http"):
            st.error("الرابط يجب أن يبدأ بـ http أو https.")
        elif any(t["name"] == nm for t in st.session_state.targets):
            st.error("هذا الاسم مستخدم. أضف كلمة مميزة (مثل اسم المتجر) ليكون مختلفاً.")
        else:
            st.session_state.targets.append({"name": nm, "url": ur})
            ok, msg = save_targets_to_github(st.session_state.targets)
            if ok:
                st.success(f"تمت إضافة «{nm}» وحفظه ✅")
            else:
                st.session_state.targets.pop()  # تراجع لو فشل الحفظ
                st.error(msg)

    st.divider()
    st.subheader("📋 منتجاتي الحالية")

    if not st.session_state.targets:
        st.info("لا توجد منتجات بعد. أضف أول منتج من الأعلى.")
    else:
        for i, t in enumerate(st.session_state.targets):
            c1, c2 = st.columns([5, 1])
            with c1:
                st.markdown(f"**{t['name']}**")
                st.markdown(f"[{t['url'][:55]}…]({t['url']})")
            with c2:
                if st.button("🗑️ حذف", key=f"del_{i}", use_container_width=True):
                    removed = st.session_state.targets.pop(i)
                    ok, msg = save_targets_to_github(st.session_state.targets)
                    if ok:
                        st.success(f"حُذف «{removed['name']}»")
                        st.rerun()
                    else:
                        st.session_state.targets.insert(i, removed)  # تراجع لو فشل
                        st.error(msg)
