import sqlite3
from datetime import datetime, date

import pandas as pd
import streamlit as st

TARGET = 1_500_000
DB_PATH = "salaries.db"

st.set_page_config(page_title="給料トラッカー", page_icon="💰", layout="centered")
st.title("💰 給料トラッカー（150万円まで）")


def get_conn():
    # check_same_thread=False は Streamlit の再実行と相性が良いことが多い
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS salaries (
                month TEXT PRIMARY KEY,
                salary INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def month_str_from_date(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def upsert_month(month: str, salary: int):
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO salaries (month, salary, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(month) DO UPDATE SET
                salary=excluded.salary,
                updated_at=excluded.updated_at
            """,
            (month, int(salary), now),
        )
        conn.commit()


def delete_month(month: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM salaries WHERE month = ?", (month,))
        conn.commit()


def load_data() -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT month, salary, updated_at FROM salaries ORDER BY month",
            conn,
        )
    if df.empty:
        return df
    df["month"] = df["month"].astype(str)
    df["salary"] = pd.to_numeric(df["salary"], errors="coerce").fillna(0).astype(int)
    return df


def build_timeseries(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["month_date"] = pd.to_datetime(out["month"] + "-01", format="%Y-%m-%d", errors="coerce")
    out = out.dropna(subset=["month_date"]).sort_values("month_date").reset_index(drop=True)
    out["cumulative"] = out["salary"].cumsum()
    return out


# 初期化
init_db()
df = load_data()

# ===== サイドバー：削除 =====
st.sidebar.header("操作")
if df.empty:
    st.sidebar.info("削除はデータがあるときに使えます．")
else:
    months = df["month"].tolist()
    del_month = st.sidebar.selectbox("削除する月を選択", months)
    if st.sidebar.button("選択した月を削除", use_container_width=True):
        delete_month(del_month)
        st.sidebar.success(f"{del_month} を削除しました")
        df = load_data()

st.sidebar.divider()
st.sidebar.caption(f"保存先：{DB_PATH}（SQLite）")

# ===== 入力（上書き） =====
with st.form("input_form", clear_on_submit=False):
    st.subheader("入力（同じ月は上書き）")

    today = date.today()
    picked = st.date_input("対象月（任意の日でOK）", value=date(today.year, today.month, 1))
    month = month_str_from_date(picked)

    default_salary = 0
    if not df.empty and (df["month"] == month).any():
        default_salary = int(df.loc[df["month"] == month, "salary"].iloc[0])

    salary = st.number_input("月々の給料（円）", min_value=0, step=1000, value=default_salary)
    submitted = st.form_submit_button("保存（上書き）")

if submitted:
    upsert_month(month, int(salary))
    st.success(f"{month} を保存しました（上書き）")
    df = load_data()

st.divider()

# ===== 進捗 =====
st.subheader("進捗")
total = int(df["salary"].sum()) if not df.empty else 0
diff = TARGET - total
progress = min(max(total / TARGET, 0.0), 1.0) if TARGET > 0 else 0.0

st.progress(progress)

c1, c2, c3 = st.columns(3)
c1.metric("合計（円）", f"{total:,}")
if diff >= 0:
    c2.metric("150万円まで残り（円）", f"{diff:,}")
else:
    c2.metric("150万円を超過（円）", f"{abs(diff):,}")
c3.metric("達成率", f"{progress * 100:.1f}%")

st.divider()

# ===== 履歴 =====
st.subheader("履歴（月ごとに1行）")
if df.empty:
    st.info("まだデータがありません．上のフォームから追加してください．")
else:
    st.dataframe(df, use_container_width=True)

st.divider()

# ===== グラフ =====
st.subheader("グラフ")
if df.empty:
    st.info("グラフはデータを追加すると表示されます．")
else:
    ts = build_timeseries(df)

    st.caption("累計（cumulative）の推移")
    line_df = ts[["month_date", "cumulative"]].rename(columns={"month_date": "month"}).set_index("month")
    st.line_chart(line_df)

    st.caption("月別の給料（salary）")
    bar_df = ts[["month_date", "salary"]].rename(columns={"month_date": "month"}).set_index("month")
    st.bar_chart(bar_df)