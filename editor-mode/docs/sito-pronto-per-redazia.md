# Preparare il proprio sito per Redazia

Redazia scrive gli articoli e li pubblica sul tuo sito, a mano (premi
"Pubblica" su una bozza approvata) o da sola, con la redazione automatica.
Questa guida spiega cosa deve avere il sito perché gli articoli arrivino
al posto giusto e si vedano come gli altri.

Ci sono quattro strade. Scegli la prima che vale per il tuo sito.

| Il tuo sito è… | Destinazione in Redazia | Cosa devi fare |
|---|---|---|
| WordPress | WordPress | Un utente e una password per le applicazioni |
| Statico su GitHub (Astro, Hugo, Next, Jekyll, Eleventy…) pubblicato da Vercel, Netlify o GitHub Pages | GitHub | Un token del repository e la cartella degli articoli |
| Un'altra piattaforma con un tuo programmatore | Webhook | Un indirizzo che riceve l'articolo in JSON |
| Nessuno dei precedenti | Un sito nuovo dal modello Redazia, su `news.tuodominio.it` o sotto `tuodominio.it/news` | Vedi l'ultima sezione |

Le destinazioni si aggiungono in **Pubblicazioni**. Se il sito è in più
lingue, aggiungi una destinazione per lingua e indica la lingua: la
traduzione di un articolo va da sola alla destinazione della sua lingua.

## WordPress

1. In WordPress crea un utente con ruolo **Autore** (può pubblicare e
   caricare immagini). Non usare l'amministratore.
2. Nel profilo di quell'utente, sotto **Password per le applicazioni**,
   crea una password chiamata "Redazia" e copiala.
3. In Redazia › Pubblicazioni aggiungi una destinazione WordPress con
   l'indirizzo del sito (`https://tuosito.it`), il nome utente e la
   password.

Il sito deve rispondere su `https://tuosito.it/wp-json/` (è attivo di
serie; alcuni plugin di sicurezza lo chiudono: va lasciato aperto per
quell'utente). Redazia invia titolo, testo, riassunto, slug e immagine di
copertina come immagine in evidenza. Con l'opzione `jsonld` aggiunge i dati
strutturati per Google, utile se non usi già Yoast o Rank Math.

## Sito statico su GitHub

Funziona con qualsiasi generatore che legge gli articoli da file Markdown
(`.md` o `.mdx`) con un'intestazione YAML tra due righe `---`.

**Cosa deve avere il repository**

- Una cartella degli articoli, per esempio `src/content/blog`. Se il sito
  è in due lingue, una cartella per lingua (`src/content/articles/it`,
  `src/content/articles/en`).
- Almeno due o tre articoli già scritti, se ci sono: Redazia li legge e
  copia il loro formato (nomi dei campi e loro ordine, formato della data,
  `.md` o `.mdx`, articolo in un file o in una cartella con l'immagine,
  dove stanno le copertine). Se la cartella è vuota usa il formato
  standard qui sotto.
- Il sito deve ricostruirsi da solo a ogni commit sul ramo principale
  (Vercel e Netlify lo fanno di serie).

**Formato standard** (quello che usa il modello Redazia)

```yaml
---
title: "Titolo dell'articolo"
description: "Descrizione per Google, al massimo 160 caratteri"
slug: "titolo-dell-articolo"
date: 2026-10-29T08:00:00Z
lang: "it"
section: "Tecnologia"
keywords: ["parola chiave", "altra"]
image: "/images/titolo-dell-articolo.webp"
image_alt: "Cosa mostra l'immagine"
---
```

Il testo segue in Markdown: sottotitoli `##`, link, elenchi, note
`[^1]` se le fonti sono mostrate come note. Il tuo modello deve mostrare il
Markdown così com'è (in Astro con `render()`, in Hugo con `.Content`).
Campi che i tuoi articoli chiamano diversamente (`pubDate`, `heroImage`,
`cover`, `category`…) vengono riconosciuti e riempiti con il nome che usi.

**Token GitHub**

Crea un token "fine-grained" su GitHub › Settings › Developer settings ›
Personal access tokens, limitato al solo repository del sito, con i
permessi **Contents: Read and write**, **Pull requests: Read and write** e
**Commit statuses: Read**. Incollalo nella destinazione.

**Destinazione in Redazia**

- Repository: `proprietario/nome`
- Cartella degli articoli: per esempio `src/content/blog`
- Indirizzo degli articoli: per esempio `https://tuosito.it/blog/{slug}`
  (serve per il link vero all'articolo, la sitemap e `llms.txt`)
- Lingua: se il sito ha più lingue

Ogni articolo è un solo commit (testo, copertina, riga in `llms.txt`,
sitemap se è un file), quindi un solo deploy. Redazia apre una pull
request e la unisce solo quando l'anteprima di Vercel o Netlify è verde:
un articolo che romperebbe il sito non arriva mai in produzione.

**Cosa conviene che il sito abbia**

- La pagina dell'articolo con titolo, data, rubrica e immagine.
- Un elenco degli ultimi articoli in home e uno per rubrica.
- Sitemap, feed RSS e `llms.txt` generati alla build.
- Il riquadro che dice che l'articolo è scritto con l'AI, se usi la
  redazione automatica (Redazia mette comunque l'etichetta nel testo).

## Webhook

Per un CMS fatto su misura o non ancora supportato. Redazia manda una
richiesta `POST` in JSON all'indirizzo che indichi, firmata con il segreto
della destinazione nell'intestazione
`X-Editor-Signature: sha256=<HMAC-SHA256 del corpo>`. Verifica sempre la
firma prima di pubblicare.

```json
{
  "event": "publication",
  "mode": "publish",
  "publication_id": 42,
  "title": "…", "subtitle": "…",
  "markdown": "…", "html": "…",
  "language": "it",
  "slug": "…", "section": "Tecnologia",
  "seo_title": "…", "meta_description": "…", "keyphrase": "…",
  "jsonld": [ … ],
  "image": {"url": "https://…", "alt": "…", "type": "image/png"}
}
```

`mode` è `publish` (pubblica) o `draft` (salva come bozza). Rispondi con
un codice 2xx quando l'articolo è salvato; con un errore la pubblicazione
risulta fallita in Pubblicazioni, con il motivo.

## Nessuna delle tre: un sito nuovo accanto al tuo

Se la tua piattaforma non permette di scrivere da fuori (Squarespace, molti
costruttori di siti), crea il blog dal **modello Redazia** e collegalo con
la destinazione GitHub. Hai due modi per metterlo sotto il tuo nome:

- **Sottodominio** `news.tuodominio.it`: un record CNAME verso Vercel o
  Netlify. È il più semplice.
- **Cartella** `tuodominio.it/news`: il tuo sito gira le richieste di
  `/news` al blog (regola di proxy su Cloudflare, Nginx, Apache, Vercel o
  Netlify). Per Google gli articoli fanno parte del tuo sito principale,
  quindi è la scelta migliore per il posizionamento. Il blog va costruito
  con `base: /news`.

## Prima di lanciare la redazione automatica

1. Pubblica un articolo a mano e controllalo sul sito: impaginazione,
   copertina, link, rubrica.
2. Se il sito è in due lingue, pubblica anche la traduzione.
3. Solo dopo accendi la redazione automatica in Sito e profilo.
