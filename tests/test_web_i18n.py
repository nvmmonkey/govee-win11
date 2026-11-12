"""Bilingual UI, web side (Task 19): every key used by index.html / app.js exists in
both the zh and the en table of web/i18n.js, the tables match, and no Chinese text is
left outside the dictionary."""
import json
import re
import string
from pathlib import Path

import pytest

from govee_win11 import config, palette
from govee_win11.effects import EFFECTS
from govee_win11.scenes import load_scenes

WEB = Path(__file__).resolve().parent.parent / "govee_win11" / "web"
I18N_JS = (WEB / "i18n.js").read_text(encoding="utf-8")
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
HAN = re.compile(r"[一-鿿]")


def _block(start: str, end: str):
    i = I18N_JS.index(start) + len(start)
    return json.loads(I18N_JS[i:I18N_JS.index(end, i)])


TABLES = _block("const TABLES = ", "; // end TABLES")
ZH, EN = TABLES["zh"], TABLES["en"]
SCENE_NAMES_ZH = _block("const SCENE_NAMES_ZH = ", "; // end SCENE_NAMES_ZH")
SCENE_CATS_ZH = _block("const SCENE_CATS_ZH = ", "; // end SCENE_CATS_ZH")


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    return "\n".join(re.sub(r"(^|[^:\"'])//.*$", r"\1", line) for line in src.split("\n"))


# ---- the tables -----------------------------------------------------------------

def test_only_zh_and_en_tables():
    assert set(TABLES) == {"zh", "en"}


def test_tables_have_identical_key_sets():
    assert set(ZH) - set(EN) == set(), "missing in en"
    assert set(EN) - set(ZH) == set(), "missing in zh"


def test_no_empty_texts():
    for lang, table in TABLES.items():
        for k, v in table.items():
            assert isinstance(v, str) and v.strip(), (lang, k)


def test_placeholders_match():
    for k in ZH:
        ph = lambda s: set(re.findall(r"\{(\w+)\}", s))  # noqa: E731
        assert ph(ZH[k]) == ph(EN[k]), k


def test_plural_variants_have_a_base_key():
    for k in ZH:
        if k.endswith(".one"):
            assert k[:-4] in ZH, k


def test_english_table_is_english():
    allowed = {"settings.language", "settings.langZh"}  # 「Language / 语言」, 「中文」
    for k, v in EN.items():
        if k not in allowed:
            assert not HAN.search(v), (k, v)


@pytest.mark.parametrize("key,text", [
    ("nav.segments", "Segments"), ("seg.palette", "Palette"), ("nav.scenes", "Scenes"),
    ("nav.screen", "Screen sync"), ("nav.music", "Music"), ("nav.settings", "Settings"),
    ("screen.transition", "Transition time"),
    ("settings.cal", "Colour calibration (white balance)"), ("settings.powerSync", "Power sync"),
])
def test_english_terminology(key, text):
    assert EN[key] == text


# ---- index.html -------------------------------------------------------------------

HTML_KEYS = {
    "data-i18n": re.findall(r'\sdata-i18n="([^"]+)"', INDEX),
    "data-i18n-title": re.findall(r'\sdata-i18n-title="([^"]+)"', INDEX),
    "data-i18n-placeholder": re.findall(r'\sdata-i18n-placeholder="([^"]+)"', INDEX),
}


def test_html_uses_the_dictionary():
    assert len(HTML_KEYS["data-i18n"]) > 100
    assert HTML_KEYS["data-i18n-title"] and HTML_KEYS["data-i18n-placeholder"]


@pytest.mark.parametrize("attr", sorted(HTML_KEYS))
def test_every_html_key_exists_in_both_tables(attr):
    for key in HTML_KEYS[attr]:
        assert key in ZH and key in EN, (attr, key)


def test_html_fallback_text_is_the_chinese_text():
    """The Chinese written in the HTML (shown before the script runs) is exactly zh."""
    pairs = re.findall(r'<(\w+)[^>]*\sdata-i18n="([^"]+)"[^>]*>([^<]*)</\1>', INDEX)
    assert len(pairs) == len(HTML_KEYS["data-i18n"])  # every data-i18n element holds text only
    for _tag, key, text in pairs:
        assert text == ZH[key], key
    for attr, html_attr in (("data-i18n-title", "title"),
                            ("data-i18n-placeholder", "placeholder")):
        for tag in re.findall(rf'<[^>]*\s{attr}="[^"]+"[^>]*>', INDEX):
            key = re.search(rf'\s{attr}="([^"]+)"', tag).group(1)
            m = re.search(rf'\s{html_attr}="([^"]*)"', tag)
            if m:
                assert m.group(1) == ZH[key], key


def test_no_chinese_left_outside_the_dictionary_in_html():
    html = re.sub(r"<!--.*?-->", "", INDEX, flags=re.S)
    html = re.sub(r'(<(\w+)[^>]*\sdata-i18n="[^"]+"[^>]*>)[^<]*(</\2>)', r"\1\3", html)
    for attr, html_attr in (("data-i18n-title", "title"),
                            ("data-i18n-placeholder", "placeholder")):
        html = re.sub(rf'(<[^>]*\s{attr}="[^"]+"[^>]*>)',
                      lambda m: re.sub(rf'\s{html_attr}="[^"]*"', "", m.group(1)), html)
    left = [line.strip() for line in html.splitlines() if HAN.search(line)]
    assert left == []


def test_language_card_has_auto_zh_en():
    assert 'id="langCtl"' in INDEX
    for lang in ("auto", "zh", "en"):
        assert f'data-lang="{lang}"' in INDEX
    assert ZH["settings.language"] == "语言 / Language"
    assert (ZH["settings.langAuto"], ZH["settings.langZh"], ZH["settings.langEn"]) == \
        ("自动", "中文", "English")


def test_i18n_script_loads_before_app():
    assert INDEX.index('src="i18n.js"') < INDEX.index('src="app.js"')


# ---- app.js -------------------------------------------------------------------------

APP_CODE = _strip_js_comments(APP_JS)
LITERAL_KEYS = set(re.findall(r'\bt\(\s*"([\w.]+)"\s*[,)]', APP_CODE)) | \
    set(re.findall(r'I18N\.has\(\s*"([\w.]+)"\s*\)', APP_CODE))


def test_app_uses_the_dictionary():
    assert len(LITERAL_KEYS) > 100


def test_every_literal_key_in_app_js_exists_in_both_tables():
    missing = sorted(k for k in LITERAL_KEYS if k not in ZH or k not in EN)
    assert missing == []


def test_no_chinese_strings_left_in_app_js():
    left = [line.strip() for line in APP_CODE.splitlines() if HAN.search(line)]
    assert left == []


def test_dynamic_key_prefixes_are_complete():
    """Keys app.js builds as prefix + id: every id the backend can send has a text."""
    prefixes = set(re.findall(r'\bt\(\s*"([\w.]+\.)"\s*\+', APP_CODE)) | \
        set(re.findall(r'I18N\.has\(\s*"([\w.]+\.)"\s*\+', APP_CODE))
    assert {"fx.", "status.", "screen.mode.", "screen.help.", "rec.style.", "rec.mood.",
            "rec.method."} <= prefixes
    ids = {
        "fx.": list(EFFECTS),
        "status.": ["connected", "connecting", "disconnected", "busy_elsewhere",
                    "bt_unavailable", "not_found"],
        "banner.": ["busy_elsewhere", "bt_unavailable", "not_found"],
        "screen.mode.": list(config.SCREEN_MODES),
        "screen.help.": list(config.SCREEN_MODES),
        "rec.style.": list(palette.STYLES),
        "rec.mood.": list(palette.MOODS),
        "rec.method.": list(palette.METHODS),
    }
    for prefix, values in ids.items():
        for v in values:
            assert prefix + v in ZH and prefix + v in EN, prefix + v


def test_link_status_literal_matches_the_statuses_shown():
    from typing import get_args
    from govee_win11.link import Status
    for st in get_args(Status):
        assert f"status.{st}" in ZH


def test_effect_names_keep_the_chinese_labels():
    for eid, label in EFFECTS.items():
        assert ZH["fx." + eid] == label


def test_banner_texts_match_the_python_link_messages():
    from govee_win11 import i18n
    for st in ("busy_elsewhere", "bt_unavailable", "not_found"):
        for lang in ("zh", "en"):
            assert TABLES[lang]["banner." + st] == i18n.tr(lang, "link." + st)


# ---- scene catalogue ----------------------------------------------------------------

def test_scene_catalogue_names_are_english_with_chinese_display_names():
    scenes = load_scenes()
    clean = lambda s: s.replace(" ", " ").strip()  # noqa: E731
    names = {clean(s.name) for s in scenes}
    assert all(not HAN.search(n) for n in names)            # the catalogue is English
    untranslated = {"MDL"}                                  # a team acronym: as is
    missing = sorted(n for n in names - untranslated if n not in SCENE_NAMES_ZH)
    assert missing == [], "no Chinese name for these scenes"
    cats = {clean(s.category) for s in scenes}
    assert cats <= set(SCENE_CATS_ZH)


def test_no_owner_specific_placement_wording():
    """The UI describes the strip itself, not one person's desk (Task 20 Part E)."""
    for word in ("桌", "电脑后", "显示器后", "背面"):
        assert word not in I18N_JS and word not in INDEX, word
    for word in ("desk", "behind the monitor"):
        assert word not in I18N_JS.lower() and word not in INDEX.lower(), word
    assert "第 1 段（灯带起点）" in ZH["seg.sub"]
    assert "Segment 1 (strip start)" in EN["seg.sub"]
    assert ZH["facts.segmentsValue"] == "{n} 段（第 1 段 = 灯带起点）"


def test_choose_other_hint_matches_python_and_not_found_is_styled_as_trouble():
    from govee_win11 import i18n
    for lang in ("zh", "en"):
        assert TABLES[lang]["banner.chooseOther"] == i18n.tr(lang, "link.choose_other")
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert css.count('.conn[data-status="not_found"]') == 2
