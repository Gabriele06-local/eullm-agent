"""The news base: general sources from around the world (world affairs,
politics, economy, technology), in English and in the languages of the
dashboard, read once an hour in the shared archive for every newsroom,
whatever sources its sites follow.

They feed the trend of the moment and its index (trends.py), and the news
the newsroom can propose (picks.py): the national news of a site's country
come from the sources in its language, the international ones from those
in English. Each feed was checked to answer before being listed here
(October 2026); one that stops answering is suspended by the archive's
maintenance, like any source.
"""

from __future__ import annotations

from sqlalchemy import select

from . import models as m
from . import repo

SECTIONS = ("world", "economy", "tech")

# (name, feed, language, section)
BASE = [
    ("BBC News · World", "https://feeds.bbci.co.uk/news/world/rss.xml", "en", "world"),
    ("BBC News · Business", "https://feeds.bbci.co.uk/news/business/rss.xml", "en", "economy"),
    ("BBC News · Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml", "en", "tech"),
    ("The Guardian · World", "https://www.theguardian.com/world/rss", "en", "world"),
    ("The Guardian · Business", "https://www.theguardian.com/uk/business/rss", "en", "economy"),
    ("The Guardian · Technology", "https://www.theguardian.com/uk/technology/rss", "en", "tech"),
    ("The New York Times · World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "en", "world"),
    ("The New York Times · Business", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", "en", "economy"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "en", "world"),
    ("Deutsche Welle", "https://rss.dw.com/rdf/rss-en-all", "en", "world"),
    ("France 24", "https://www.france24.com/en/rss", "en", "world"),
    ("Euronews", "https://www.euronews.com/rss", "en", "world"),
    ("Euractiv", "https://www.euractiv.com/feed/", "en", "world"),
    ("Financial Times", "https://www.ft.com/rss/home", "en", "economy"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "en", "economy"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "en", "tech"),
    ("The Verge", "https://www.theverge.com/rss/index.xml", "en", "tech"),
    ("TechCrunch", "https://techcrunch.com/feed/", "en", "tech"),
    ("Wired", "https://www.wired.com/feed/rss", "en", "tech"),
    ("MIT Technology Review", "https://www.technologyreview.com/feed/", "en", "tech"),
    ("The Register", "https://www.theregister.com/headlines.atom", "en", "tech"),
    ("Engadget", "https://www.engadget.com/rss.xml", "en", "tech"),
    ("Hacker News", "https://hnrss.org/frontpage", "en", "tech"),
    ("BleepingComputer", "https://www.bleepingcomputer.com/feed/", "en", "tech"),
    ("The Hacker News", "https://feeds.feedburner.com/TheHackersNews", "en", "tech"),
    ("ANSA", "https://www.ansa.it/sito/ansait_rss.xml", "it", "world"),
    ("ANSA · Mondo", "https://www.ansa.it/sito/notizie/mondo/mondo_rss.xml", "it", "world"),
    ("ANSA · Economia", "https://www.ansa.it/sito/notizie/economia/economia_rss.xml", "it", "economy"),
    ("ANSA · Tecnologia", "https://www.ansa.it/sito/notizie/tecnologia/tecnologia_rss.xml", "it", "tech"),
    ("Corriere della Sera", "https://xml2.corriereobjects.it/rss/homepage.xml", "it", "world"),
    ("la Repubblica", "https://www.repubblica.it/rss/homepage/rss2.0.xml", "it", "world"),
    ("Il Sole 24 Ore · Economia", "https://www.ilsole24ore.com/rss/economia.xml", "it", "economy"),
    ("Wired Italia", "https://www.wired.it/feed/rss", "it", "tech"),
    ("Punto Informatico", "https://www.punto-informatico.it/feed/", "it", "tech"),
    ("HDblog", "https://www.hdblog.it/feed/", "it", "tech"),
    ("tagesschau", "https://www.tagesschau.de/xml/rss2/", "de", "world"),
    ("Der Spiegel", "https://www.spiegel.de/schlagzeilen/index.rss", "de", "world"),
    ("Handelsblatt", "https://www.handelsblatt.com/contentexport/feed/schlagzeilen", "de", "economy"),
    ("heise online", "https://www.heise.de/rss/heise-atom.xml", "de", "tech"),
    ("Le Monde", "https://www.lemonde.fr/rss/une.xml", "fr", "world"),
    ("France 24 · Français", "https://www.france24.com/fr/rss", "fr", "world"),
    ("Le Monde · Pixels", "https://www.lemonde.fr/pixels/rss_full.xml", "fr", "tech"),
    ("El País", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada", "es", "world"),
    ("BBC News Mundo", "https://feeds.bbci.co.uk/mundo/rss.xml", "es", "world"),
    ("Expansión", "https://e00-expansion.uecdn.es/rss/portada.xml", "es", "economy"),
    ("Xataka", "https://www.xataka.com/feedburner.xml", "es", "tech"),
]


def ensure(db, archive: str) -> set[int]:
    """The base sources in the archive, added when missing; their ids."""
    with db.tenant(archive) as s:
        # Told apart by their config: {"base": true, ...}.
        return {repo.archive_source(s, kind="rss", name=name, url=url,
                                    config={"base": True, "language": lang, "section": section})
                for name, url, lang, section in BASE}


def sections(s, source_ids) -> dict[int, str]:
    """The section of each base source among `source_ids`."""
    src = m.sources
    rows = s.execute(select(src.c.id, src.c.config).where(src.c.id.in_(list(source_ids))))
    return {r.id: r.config["section"] for r in rows if (r.config or {}).get("base") and r.config.get("section")}
