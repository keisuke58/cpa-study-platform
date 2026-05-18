"""
Bilingual (日本語 / English) translation helper.

Usage:
    from i18n import t, LANG_KEY
    lang = st.session_state.get(LANG_KEY, "ja")
    st.header(t("ai_title", lang))
"""

LANG_KEY = "lang"

_T: dict[str, dict[str, str]] = {
    # ── Navigation ───────────────────────────────────────────────
    "nav_dashboard":   {"ja": "Dashboard 📊",           "en": "Dashboard 📊"},
    "nav_syllabus":    {"ja": "My Syllabus 📚",          "en": "My Syllabus 📚"},
    "nav_checklist":   {"ja": "Official Checklist ✅",   "en": "Official Checklist ✅"},
    "nav_revisions":   {"ja": "Revisions 🧭",            "en": "Revisions 🧭"},
    "nav_vocab":       {"ja": "Vocabulary 📖",           "en": "Vocabulary 📖"},
    "nav_formulas":    {"ja": "Formulas 📐",             "en": "Formulas 📐"},
    "nav_english":     {"ja": "English Prep 🌐",         "en": "English Prep 🌐"},
    "nav_old_exams":   {"ja": "Old Exams 📄",            "en": "Old Exams 📄"},
    "nav_timer":       {"ja": "Study Timer ⏱️",          "en": "Study Timer ⏱️"},
    "nav_mock":        {"ja": "Mock Exams 📝",           "en": "Mock Exams 📝"},
    "nav_scores":      {"ja": "Scores 📈",               "en": "Scores 📈"},
    "nav_wrong":       {"ja": "Wrong Answers 📕",        "en": "Wrong Answers 📕"},
    "nav_drills":      {"ja": "Drills 🔧",               "en": "Drills 🔧"},
    "nav_exam_mode":   {"ja": "Exam Mode ⏲️",            "en": "Exam Mode ⏲️"},
    "nav_survival":    {"ja": "Survival Mode ⚡",         "en": "Survival Mode ⚡"},
    "nav_analytics":   {"ja": "Analytics 📊",            "en": "Analytics 📊"},
    "nav_roadmap":     {"ja": "Roadmap 🗺️",              "en": "Roadmap 🗺️"},
    "nav_big4":        {"ja": "Big 4 Job Hunting 💼",    "en": "Big 4 Job Hunting 💼"},
    "nav_company":     {"ja": "Company Directory 🏢",    "en": "Company Directory 🏢"},
    "nav_edinet":      {"ja": "EDINET 🧾",               "en": "EDINET 🧾"},
    "nav_future":      {"ja": "Future 🚀",               "en": "Future 🚀"},
    "nav_ai_qa":       {"ja": "AI Q&A 🤖",               "en": "AI Q&A 🤖"},
    "nav_smart":       {"ja": "スマート問題集 📝",         "en": "Smart Drills 📝"},

    # ── Exam type selector ───────────────────────────────────────
    "exam_type_label": {"ja": "試験種別",                "en": "Exam Type"},
    "exam_cpa_jp":     {"ja": "🇯🇵 日本CPA",             "en": "🇯🇵 Japan CPA"},
    "exam_uscpa":      {"ja": "🇺🇸 USCPA",               "en": "🇺🇸 USCPA"},

    # ── Subject names ────────────────────────────────────────────
    "subj_financial":  {"ja": "財務会計論",              "en": "Financial"},
    "subj_management": {"ja": "管理会計論",              "en": "Management"},
    "subj_audit":      {"ja": "監査論",                  "en": "Audit"},
    "subj_company":    {"ja": "企業法",                  "en": "Company Law"},
    "subj_far":        {"ja": "FAR（財務会計）",         "en": "FAR (Financial Accounting)"},
    "subj_aud":        {"ja": "AUD（監査）",             "en": "AUD (Auditing)"},
    "subj_reg":        {"ja": "REG（税務・法規）",       "en": "REG (Regulation)"},
    "subj_bar":        {"ja": "BAR（ビジネス分析）",     "en": "BAR (Business Analysis)"},

    # ── AI Q&A tab ───────────────────────────────────────────────
    "ai_title":        {"ja": "CPA AI アシスタント 🤖",  "en": "CPA AI Assistant 🤖"},
    "ai_caption_jp":   {"ja": "studying.jp の講座資料を基に、CPA 試験の質問に答えます。",
                        "en": "Answers CPA exam questions based on your studying.jp course materials."},
    "ai_caption_us":   {"ja": "USCPA の問題に英語で回答します。",
                        "en": "Answers USCPA questions in English."},
    "ai_provider":     {"ja": "LLM プロバイダー",        "en": "LLM Provider"},
    "ai_model":        {"ja": "モデル",                  "en": "Model"},
    "ai_top_k":        {"ja": "取得チャンク数",          "en": "Retrieved chunks"},
    "ai_show_src":     {"ja": "参照資料を表示",          "en": "Show sources"},
    "ai_build_btn":    {"ja": "📦 インデックス構築",     "en": "📦 Build Index"},
    "ai_build_help":   {"ja": "新規 PDF が追加されたときに押してください",
                        "en": "Press after new PDFs are scraped"},
    "ai_build_done":   {"ja": "インデックス構築完了！",  "en": "Index built successfully!"},
    "ai_chunks_info":  {"ja": "チャンク数",              "en": "Chunks"},
    "ai_placeholder":  {"ja": "CPA 試験について質問してください…",
                        "en": "Ask anything about the CPA exam…"},
    "ai_placeholder_us":{"ja": "USCPA について質問してください…",
                         "en": "Ask anything about the USCPA exam…"},
    "ai_thinking":     {"ja": "検索・回答生成中…",       "en": "Searching and generating answer…"},
    "ai_sources":      {"ja": "参照資料",                "en": "Sources"},
    "ai_clear":        {"ja": "🗑️ 会話をリセット",       "en": "🗑️ Clear chat"},
    "ai_error":        {"ja": "エラーが発生しました",    "en": "An error occurred"},
    "ai_no_key":       {"ja": "API キーが未設定です",    "en": "API key not set"},

    # ── Scrape monitor ───────────────────────────────────────────
    "scrape_header":   {"ja": "📥 スクレイプ進捗",       "en": "📥 Scrape Progress"},
    "scrape_total":    {"ja": "取得済み PDF",            "en": "PDFs fetched"},
    "scrape_setsumon": {"ja": "設問",                    "en": "Questions"},
    "scrape_kaitou":   {"ja": "解答",                    "en": "Answers"},
    "scrape_courses":  {"ja": "対象コース",              "en": "Courses"},

    # ── AI settings sidebar ──────────────────────────────────────
    "ai_settings":     {"ja": "AI 設定",                 "en": "AI Settings"},

    # ── Drills / Syllabus ────────────────────────────────────────
    "coming_soon":     {"ja": "🚧 データ収集中 — 近日公開",
                        "en": "🚧 Data collection in progress — coming soon"},
}


def t(key: str, lang: str = "ja") -> str:
    """Return translated string for key in given language."""
    entry = _T.get(key)
    if entry is None:
        return key
    return entry.get(lang, entry.get("ja", key))


def nav_items(lang: str = "ja") -> list[str]:
    """Return navigation item list in display strings (language-independent labels)."""
    keys = [
        "nav_dashboard", "nav_syllabus", "nav_checklist", "nav_revisions",
        "nav_vocab", "nav_formulas", "nav_english", "nav_old_exams",
        "nav_timer", "nav_mock", "nav_scores", "nav_wrong", "nav_drills",
        "nav_smart", "nav_exam_mode", "nav_survival", "nav_analytics",
        "nav_roadmap", "nav_big4", "nav_company", "nav_edinet", "nav_future",
        "nav_ai_qa",
    ]
    return [t(k, lang) for k in keys]


# USCPA subject keys (used to detect USCPA content in questions dicts)
USCPA_SUBJECTS = ["USCPA_FAR", "USCPA_AUD", "USCPA_REG", "USCPA_BAR"]
JPCPA_SUBJECTS = ["Financial", "Management", "Audit", "Company"]

SUBJECT_DISPLAY = {
    "Financial":  {"ja": "財務会計論", "en": "Financial"},
    "Management": {"ja": "管理会計論", "en": "Management"},
    "Audit":      {"ja": "監査論",     "en": "Audit"},
    "Company":    {"ja": "企業法",     "en": "Company Law"},
    "USCPA_FAR":  {"ja": "FAR",        "en": "FAR"},
    "USCPA_AUD":  {"ja": "AUD",        "en": "AUD"},
    "USCPA_REG":  {"ja": "REG",        "en": "REG"},
    "USCPA_BAR":  {"ja": "BAR",        "en": "BAR"},
}


def subject_label(subject: str, lang: str = "ja") -> str:
    entry = SUBJECT_DISPLAY.get(subject, {})
    return entry.get(lang, subject)
