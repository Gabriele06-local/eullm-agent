"""Every text marked for translation has its English, German, French and
Spanish version, with the same placeholders."""

import ast
import re
import string
from pathlib import Path

from editor import i18n

SRC = Path(i18n.__file__).parent
# _('...') or _("...") in templates; the text is a single literal.
TEMPLATE_CALL = re.compile(r"""_\(\s*(?P<q>['"])(?P<text>(?:\\.|(?!(?P=q)).)*?)(?P=q)""", re.S)


def marked() -> dict[str, str]:
    """Each marked text and where it is."""
    found = {}
    for path in sorted((SRC / "templates").glob("*.j2")):
        for m in TEMPLATE_CALL.finditer(path.read_text(encoding="utf-8")):
            text = m.group("text").replace("\\'", "'").replace('\\"', '"')
            found.setdefault(text, path.name)
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("t", "N_", "_")
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                found.setdefault(node.args[0].value, path.name)
    return found


def fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def test_every_marked_text_is_translated():
    texts = marked()
    assert len(texts) > 50
    for lang in ("en", "de", "fr", "es"):
        cat = i18n.catalog(lang)
        missing = [f"{where}: {text[:70]}" for text, where in texts.items() if not cat.get(text)]
        assert not missing, f"{lang}: {len(missing)} missing, e.g. " + "; ".join(missing[:5])
        wrong = [text[:70] for text in texts if text in cat and fields(cat[text]) != fields(text)]
        assert not wrong, f"{lang}: placeholders differ in " + "; ".join(wrong[:5])


def test_language_comes_from_the_cookie_then_the_browser():
    assert i18n.pick("de", "fr-FR,fr;q=0.9") == "de"
    assert i18n.pick(None, "fr-FR,fr;q=0.9,en;q=0.8") == "fr"
    assert i18n.pick("xx", "pt-BR,es;q=0.5") == "es"
    assert i18n.pick(None, None) == "it"  # EDITOR_LANGUAGE in the tests


def test_texts_follow_the_language():
    token = i18n.use("it")
    try:
        assert i18n.t("Oggi") == "Oggi"
        i18n.use("en")
        assert i18n.t("Oggi") == i18n.catalog("en").get("Oggi", "Oggi")
        assert str(i18n.gettext("<b>{x}</b>", x="<i>")) == "<b>&lt;i&gt;</b>"
    finally:
        i18n._current.reset(token)


def test_every_page_renders_in_every_language(db, tenant):
    from test_publishing import approved_draft
    from test_ui import PAGES, logged_in

    did, _ = approved_draft(db, tenant)
    c = logged_in(db, tenant, "owner")
    for lang in i18n.LANGS:
        assert c.get(f"/lang?l={lang}&back=/", follow_redirects=False).status_code == 303
        for path in PAGES + [f"/drafts/{did}", "/account"]:
            r = c.get(path)
            assert r.status_code == 200, (lang, path)
            assert f'<html lang="{lang}">' in r.text, (lang, path)
    page = c.get("/").text  # Spanish, the last one chosen
    assert "Fuentes</a>" in page and "Fonti</a>" not in page
