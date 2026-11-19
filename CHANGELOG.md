# Changelog

## 0.38.0

### Added

- **Rubriche decise dalla redazione** (Francesco, 19 novembre, con i suoi
  paletti). Ogni lunedì, per un sito con la redazione automatica, la
  redazione guarda gli argomenti in tendenza nell'archivio nella settimana
  appena finita (non gli articoli del sito), il modello li assegna alle
  rubriche del sito o a nessuna se sono fuori contesto, e ogni rubrica pesa
  quanto il suo hype: più posti al giorno alle più calde, meno alle altre
  (il massimo per rubrica per il peso, tra un quarto e il doppio). I limiti:
  - le rubriche del direttore non vengono mai tolte: scendono al massimo a
    un articolo al giorno e risalgono la settimana in cui la notizia torna;
  - al massimo una rubrica nuova a settimana, 3 in tutto, solo su un
    argomento in tendenza da almeno 3 giorni, dentro il contesto e mai
    sugli argomenti da non trattare; diventa anche un tema della linea, così
    le sue notizie arrivano;
  - i cambi sono nel rapporto del lunedì e in Sito e profilo › Redazione
    automatica › "Rubriche decise dalla redazione", con la priorità di ogni
    rubrica, "Annulla l'ultimo cambio", "Togli" per una rubrica aggiunta
    (non torna più) e l'interruttore per sito, acceso da solo solo con la
    redazione automatica.
  Su un sito collegato con GitHub una rubrica nuova ha la sua pagina quando
  la si aggiunge anche alla configurazione del sito; su WordPress la
  categoria si crea da sola.

### Changed

- **La rianalisi settimanale non impara più la linea dagli articoli scritti
  dalla redazione** (Francesco, 19 novembre, su aitoday.it: "un cane che si
  morde la coda"). Su un sito scritto da Redazia la rianalisi misurava il
  proprio lavoro: i temi più trattati crescevano, gli altri uscivano e la
  linea si restringeva da sola all'hype della settimana. Ora:
  - gli articoli pubblicati dalla redazione (riconosciuti da indirizzo,
    slug o titolo delle pubblicazioni) non contano per il profilo, ma
    restano tra gli articoli del sito per sapere cosa è già stato scritto;
  - con meno di 5 articoli scritti da persone la rianalisi non propone
    nessuna nuova versione: la linea resta quella approvata;
  - una nuova versione può aggiungere temi ma non toglierne: un tema lo
    toglie solo il direttore;
  - il sito non si rilegge più ogni lunedì: si legge all'inizio e quando
    premi "Rianalizza ora";
  - senza redazione automatica, ogni lunedì la direzione la danno le tue
    scelte: con almeno 10 proposte accettate nelle ultime 4 settimane e uno
    scarto di almeno 15 punti, arriva una proposta con i temi pesati come
    li hai scelti (nessun tema esce), da approvare con le spunte. Con la
    redazione automatica la linea cambia solo per mano del direttore.
- **Il confronto del profilo spiega cosa vuol dire togliere un tema e quanti
  ne restano** (Francesco, 19 novembre, su aitoday.it: "aggiunge o
  filtra?"). Sopra la tabella: le percentuali vengono dagli articoli del
  sito, i temi sono il filtro delle notizie delle fonti. Sotto: "Con queste
  scelte la redazione segue N temi", con un avviso quando sono pochi. Un
  tema che allarga il nome ("Intelligenza artificiale" → "Intelligenza
  artificiale e agenti IA") è una riga sola, non un tema nuovo più uno che
  esce, e nessun tema approvato sparisce dal confronto senza la sua riga.

## 0.37.1

### Fixed

- **I backup non crescono più di 20 MB al giorno** (Francesco, 19 novembre:
  archivi decuplicati in meno di un mese). Due cause:
  - i punteggi Hype: ogni ora una riga per ogni tema della settimana, mai
    cancellata, ma si legge solo l'ultima. Sul server erano 7,7 GB. Ora il
    calcolo orario tiene solo l'ultimo punteggio di ogni tema e la
    manutenzione notturna cancella quelli vecchi in una sola lettura della
    tabella;
  - le copertine delle traduzioni: ogni lingua teneva una copia identica
    dell'immagine (11-12 MB al giorno su aitoday.it). Ora la traduzione
    punta all'immagine dell'originale; la migrazione 0048 trasforma in
    rimandi le copie già fatte. Se l'immagine dell'originale viene
    cancellata, le traduzioni ne ricevono prima una copia.

### Changed

- **Backup: più archivi eliminati in una volta** (Francesco, 19 novembre:
  oggi bisognava cliccare ogni archivio). In Console › Backup ogni archivio
  ha una spunta, in testa una per spuntarli tutti, e "Elimina gli archivi
  spuntati" li cancella insieme dopo una conferma. Le richieste fatte prima
  che il server le esegua si sommano; `update.sh` (si aggiorna da solo con
  la versione) cancella solo nomi di archivio validi della cartella dei
  backup.

## 0.37.0

### Changed

- **Il nuovo profilo si sceglie voce per voce, senza mai toccare il JSON**
  (Francesco, 19 novembre: "Modifica" sulla proposta v4 di aitoday.it
  apriva il profilo in JSON da correggere a mano). In Sito e profilo la
  tabella "Cosa cambia rispetto alla v2 approvata" ha una spunta per ogni
  riga (settore, pubblico, tono, lingue, descrizione, ogni tema nuovo, ogni
  tema che cambia, ogni tema che esce, proposte al giorno, pertinenza),
  tutte spuntate sulla proposta. Togliendo la spunta resta il valore della
  versione approvata: un tema nuovo non entra, un tema che esce resta, un
  tema che cambia torna com'era. "Approva le scelte" crea e approva una
  nuova versione con quelle scelte, controllata dal server (se nel
  frattempo è arrivata un'altra proposta o le scelte non formano un profilo
  valido non cambia nulla e lo dice); con tutte le righe spuntate approva la
  proposta così com'è, con nessuna tiene la versione approvata.
- "Modifica il profilo" ora è un modulo con settore, pubblico e tono; i temi
  si tolgono e si rimettono dalla tabella dei temi, come prima. Il JSON
  grezzo non c'è più nelle pagine della redazione (resta solo l'API
  `PUT /api/sites/{id}/profile` per chi integra da programma).

## 0.36.7

### Fixed

- **Le email di Redazia non finiscono più nello spam per le intestazioni
  mancanti** (Francesco, 16 novembre: il rapporto settimanale di AI Today
  segnato come spam da SpamAssassin, 5,1 punti). Ogni email (inviti,
  rapporto settimanale, briefing, consegne) ora ha `Message-ID` con il
  dominio del mittente e `Date`: erano 5,5 punti su 5,1 (MISSING_MID,
  MISSING_DATE, DOS_BODY_HIGH_NO_MID).

## 0.36.6

### Fixed

- **La redazione automatica non si ferma più al controllo prima di
  scrivere** (Francesco, 15 novembre: su aitoday.it nessun articolo dalle 11
  alle 17, ogni proposta chiusa prima di essere scritta). Il controllo ora
  chiude una proposta solo per uno dei tre motivi previsti, e lo deve dire:
  stessa notizia già uscita (con il titolo di un articolo del sito che
  esiste davvero), editoriale senza fatti, raccolta di fatti diversi.
  Spiegazioni, analisi, interviste, mostre, spettacoli e decisioni di
  governi, tribunali e aziende si scrivono. Dopo tre proposte chiuse in un
  giorno per lo stesso sito il controllo si fa da parte fino al giorno
  dopo: resta il caporedattore, che legge ogni articolo.
- Nel Desk, per un sito con la redazione automatica, la colonna Proposte
  dice «N per la redazione automatica»: sono le proposte fra cui sceglie da
  sola, non aspettano te.

## 0.36.5

### Changed

- **Il trend del momento è solo quello del sito** (Francesco, 15 novembre:
  su lagrandecucina il trend era la multa a Meta con un aggancio forzato
  al tiramisù). Un tema entra nel trend del momento solo se tocca davvero
  uno degli argomenti del sito, cioè almeno due parole di un argomento del
  profilo; quelli vicini per una parola sola e alti solo per l'Hype
  restano fuori.
- **Le proposte del piano si vedono tutte**. Trend e piano mostrava solo le
  ultime 20, mentre il Desk le contava tutte (46): ora il piano le elenca
  tutte e dice quante aspettano il tuo sì. Nel Desk il riquadro dice che il
  numero esclude le pubblicate e le scartate.
- **«Pubblicate su proposte, 30 giorni» al posto del costo modello** nel
  Desk: quante proposte degli ultimi 30 giorni sono uscite come articolo,
  con la percentuale. Il costo resta in Consumi.

## 0.36.4

### Changed

- **Controllo prima di scrivere** (Francesco, 14 novembre: «4 scarti e 4
  pubblicazioni, pago sempre il doppio»). Prima di scrivere una notizia
  la redazione automatica guarda la proposta, i titoli delle notizie su cui
  si basa e gli articoli del sito degli ultimi due giorni. Se è la stessa
  storia già uscita, un editoriale o un commento senza un fatto da
  raccontare, o una raccolta di fatti diversi, la chiude senza scriverla,
  come le notizie troppo corte, e al suo posto va un'altra proposta. Prima
  queste venivano scritte, scartate dal caporedattore, riscritte dal
  modello più forte e scartate di nuovo. Il passo si chiama «Controllo
  prima di scrivere» in Consumi › Modello per passo e può andare su Haiku.
  Se il modello non risponde, la notizia si scrive come prima.
- **«Pubblica le autorizzate»** (Francesco, 14 novembre: cinque ricette
  una dietro l'altra facevano dieci build su Vercel). Sui siti collegati a
  GitHub il direttore approva e autorizza («Approva e autorizza», o
  «Pubblica» su una bozza già approvata), ma l'articolo non parte subito.
  In basso a destra compare, su ogni pagina, il riquadro «N articoli
  autorizzati da pubblicare» con il pulsante «Pubblica le autorizzate»: un
  clic e tutti vanno online in un solo commit per repository, quindi una
  build sola. Il riquadro c'è solo quando qualcosa aspetta. WordPress e
  webhook pubblicano subito come prima. Le ricette nuove di un menù partono
  ancora subito, prima del menù.
- **Copertine meno uguali** (Francesco, 14 novembre: «tavoli, computer,
  gilet gialli, sale riunioni a gogo, sempre stesso legno»). Chi descrive
  la copertina evita le scene di repertorio (sale riunioni vuote, tavoli di
  legno, portatili sulla scrivania, mani che scrivono, gilet ad alta
  visibilità, uffici generici, microfoni, telefono in mano) se l'articolo
  non parla proprio di quello. Vede le ultime 12 copertine del sito per non
  ripeterne scena, oggetti e colori, e ogni copertina ha un tipo di
  inquadratura diverso: campo largo all'aperto, dettaglio, persone in
  azione, vista dall'alto, scena di strada, oggetto simbolico, notte.
- **Le letture della redazione nel «Cosa cambia» si tagliano.** Se il
  problema è solo una lettura scritta come un fatto nell'ultima sezione, il
  caporedattore toglie quelle frasi invece di scartare l'articolo.

## 0.36.3

### Fixed

- **Il Punto della Sera restava bozza due sere di fila** (Francesco, 13
  novembre). Il direttore AI scriveva frasi come «è una lettura della
  redazione: nessuna fonte collega i due fatti», che il caporedattore
  rifiuta perché parlano delle fonti. Ora le letture si scrivono e basta,
  senza commentare le fonti, e i fatti restano nell'ordine e alle date
  degli articoli. Il caporedattore può togliere fino a dodici frasi invece
  di sei, perché il Punto della Sera è lungo.
- **Notizie di una sola testata scritte e poi scartate.** Quando una
  notizia ha una sola testata, la redazione la scrive lo stesso (le testate
  minime sono una preferenza), ma il caporedattore la scartava dopo, a
  spese pagate, perché le direttive chiedono due fonti. Ora sa che la
  notizia ha una sola testata e non la scarta per questo.

## 0.36.2

### Changed

- **Articolo e traduzioni nello stesso commit** (Francesco, 13 novembre:
  Vercel fa pagare ogni build). Quando l'originale e le sue traduzioni
  vanno nello stesso repository GitHub, la redazione automatica scrive
  prima le traduzioni e poi manda tutto in un solo commit: il sito si
  ricostruisce una volta sola invece di una per lingua. Se una traduzione
  non riesce, l'articolo esce lo stesso e quella lingua segue da sola,
  come prima. Su WordPress e webhook non cambia niente.

## 0.36.1

### Added

- **Il modello del Punto della Sera si sceglie** in /admin › Consumi ›
  «Modello per passo», come gli altri passi (prima usava sempre il
  predefinito). Era rimasto fuori dalla 0.36.0.

## 0.36.0

### Added

- **Chiedere un menù intero** (Francesco, 11 novembre: «sarebbe bello
  poter chiedere un menu»). Sui siti di ricette, «Chiedi un articolo» nel
  Desk ha la scelta «Un menù intero per un'occasione». Si scrive
  l'occasione («Menù della Vigilia di pesce», «Pranzo di Natale vegetariano
  per 8») e la redazione fa come per i menù proposti: da 3 a 5 portate,
  riusa le ricette del sito che vanno bene, scrive solo quelle che mancano
  (ognuna una bozza a sé, con foto e categoria), poi il menù esce dopo le
  sue ricette nuove. Le notizie di cucina su cui si appoggia l'apertura si
  cercano nell'ultimo mese invece che nelle ultime due settimane. Sugli
  altri siti la scelta non c'è.
## 0.35.1

### Fixed

- **Il Feedback dei lettori funziona sul server** (Francesco, 11 novembre:
  «attivato feedback ma non vedo»). Il modulo cercava il sito, e il link di
  conferma il messaggio, con la connessione del proprietario. Sul server
  quel ruolo non è un superutente e la sicurezza per riga gli nasconde i
  dati di ogni redazione, quindi ogni sito rispondeva «spento». Ora le due
  ricerche passano da due funzioni del database che guardano redazione per
  redazione (migrazione 0047). Provato con un proprietario non superutente,
  come sul server.
- **CI**: bubblewrap si installa solo se manca dall'immagine del runner
  (c'è già): l'11 novembre i mirror Ubuntu non rispondevano e il passo
  andava in timeout prima dei test, su main e su dev.

## 0.35.0

### Added

- **Feedback dei lettori, in prova e per ora gratuito** (Francesco,
  11 novembre). Sotto ogni articolo un modulo permette di segnalare un
  errore o scrivere alla redazione. Il messaggio arriva nel pannello, nella
  nuova voce «Feedback», solo dopo che il lettore ha confermato l'email
  dal link che riceve. La conferma è un bottone e non il link stesso,
  così i programmi che aprono i link nelle email non confermano al posto
  suo. Il messaggio non viene pubblicato e non va mai al modello. Ogni
  sito lo accende o spegne da Feedback, dove trova anche la riga di codice
  da mettere nel sito (`/static/feedback.js`, in 5 lingue). Il modello
  Redazia lo mette da solo con `feedback` in site.config.mjs.
  Protezioni: il modulo accetta messaggi solo dal dominio del sito (e dai
  suoi sottodomini) e solo se il sito l'ha acceso. Ci sono limiti per
  indirizzo (10 all'ora) e per email (3 all'ora), un solo messaggio in
  attesa per email e un campo trappola per i programmi. Il testo è
  semplice, fino a 2000 caratteri, e mostrato senza HTML. Con
  `EDITOR_TURNSTILE_SITEKEY` e `EDITOR_TURNSTILE_SECRET` c'è anche la
  verifica Cloudflare Turnstile. I messaggi non confermati spariscono dopo
  48 ore, gli altri dopo un anno; l'indirizzo IP non viene conservato, solo
  un'impronta cifrata. Senza server di posta il modulo non si mostra.
  Migrazione 0046 (SQLite 0035): `sites.feedback`, `reader_messages`.

## 0.34.6

### Fixed

- **Due articoli inviati insieme allo stesso repository non falliscono più**
  (Francesco, 11 novembre: «GitHub, merge: 405 Pull Request has merge
  conflicts»). Tutti e due aggiungono la loro riga a llms.txt e alla
  sitemap: la seconda pull request andava in conflitto appena la prima era
  unita. Ora viene ricostruita sul sito com'è in quel momento e riprovata,
  fino a due volte.
- **Bozze dice «Pubblicata»** per un articolo uscito (prima restava
  «Approvata»), con il suo filtro; anche la pagina della bozza mostra
  «Pubblicata» e non ripete il tentativo non riuscito quando un invio
  successivo alla stessa destinazione è andato a buon fine (resta nello
  storico di Pubblicazioni).
- **La pubblicazione si vede nel riquadro Attività** anche quando il menu è
  su un solo sito: l'invio non sapeva a quale sito appartenesse e veniva
  nascosto («ma quale riquadro io non vedo nulla»). Ora ha anche il titolo.
- **Il piano editoriale dice quando un articolo è uscito** (Francesco,
  11 novembre: due ricette pubblicate restavano «Bozza pronta»). In Trend e
  piano la riga di una bozza pubblicata mostra «Pubblicata» e il bottone
  «Apri l'articolo» con il link vero; se la pubblicazione viene ritirata
  torna «Bozza pronta». Vale per ricette e notizie.
- **Niente pubblicità spacciate per notizie** (Francesco, 11 novembre: su
  aitoday.it è uscito un articolo sull'offerta di un fornitore di luce e gas
  «in scadenza il 10 novembre», preso da un publiredazionale e pubblicato
  l'11). Le notizie marcate come contenuto sponsorizzato, publiredazionale,
  advertorial, «in collaborazione con» e simili non entrano più fra le
  candidate; proposte e selezione non propongono offerte, promozioni,
  tariffe o scadenze commerciali di una singola azienda; il caporedattore
  AI rifiuta un articolo che è in sostanza una pubblicità o che racconta
  come ancora aperta una scadenza o un'offerta già finita alla data di
  pubblicazione, che ora riceve. Le notizie di mercato, le decisioni di un
  regolatore sui prezzi e i confronti fra molte offerte restano ammessi.

## 0.34.5

### Changed

- **Titoli delle ricette propri, con il nome del piatto di sempre**
  (Francesco, 11 novembre: il titolo non deve essere quello della ricetta
  di partenza, ma «la cotoletta alla milanese si chiama così»). Le proposte
  su un tema e le ricette del sito chiamano il piatto con il suo nome
  tradizionale, mai con un nome inventato o descrittivo; il titolo però non
  può copiare quello di una fonte (le stesse parole, o quasi): se lo fa, il
  modello lo riscrive tenendo il nome del piatto e cambiando il resto. Il
  solo nome del piatto («Cotoletta alla milanese») non è mai una copia.

## 0.34.4

### Changed

- **Prezzi anche IVA inclusa** (Francesco, 10 novembre; Codice del Consumo
  art. 49 c. 1 lett. e: a un consumatore va dato il prezzo comprensivo
  delle imposte). In Il mio piano ogni piano mostra «149 € + IVA al mese»
  con sotto «181,78 € IVA inclusa», anche per l'anno; i pacchetti di
  crediti e la lingua extra hanno lo stesso doppio prezzo. Un'azienda UE
  con partita IVA, che non paga l'IVA, vede solo il prezzo e l'inversione
  contabile. `/api/public/plans` restituisce `vat_percent`, che
  redazia.com usa per la stessa riga.

## 0.34.3

### Changed

- **Proposte durante il giorno anche senza redazione automatica**
  (Francesco, 10 novembre: lagrandecucina.it ferma tutto il giorno sulle
  proposte del mattino). Dopo il briefing, ogni tre ore fino alle 20 (con
  il briefing alle 7: 10, 13, 16, 19) arriva un giro di proposte per i posti
  del giorno ancora liberi: «Proposte al giorno» del sito, meno gli
  articoli già scritti oggi, meno le proposte in attesa da meno di 6 ore.
  Quelle più vecchie restano da decidere ma lasciano il posto alle nuove.
  Quando gli articoli scritti arrivano al numero del giorno, i giri si
  fermano fino alla mattina dopo; si fermano anche a piano del mese finito
  o dopo la prova. Con la redazione automatica accesa non cambia niente.
- **Una testata non riempie più la giornata** (le quattro ricette tutte da
  Gustoblog). La quota di una testata (un terzo) vale su tutte le proposte
  delle ultime 24 ore, non solo su quelle dello stesso giro. Le proposte su
  un tema ricevono al massimo un terzo di notizie dalla stessa testata e
  devono citarne almeno due diverse. Desk e Trend e piano mostrano tutte le
  testate di una proposta, non solo quella della notizia più recente.

## 0.34.2

### Added

- **Lingua extra per sito** (Francesco, 10 novembre: eullm.eu ha 8 lingue,
  il piano più grande ne dà 5). Il direttore la compra da Sito e profilo ›
  Lingue › «Aggiungi una lingua»: paga subito con Mollie la parte del
  periodo che resta, poi la lingua si rinnova con il piano, sulla stessa
  carta (l'importo dell'abbonamento Mollie diventa piano + lingue extra, anche
  quando si cambia piano). Ogni lingua extra vale per un solo sito. «Togli una
  lingua extra» smette di farla pagare dal rinnovo dopo, senza rimborso, e
  solo se il sito non la sta usando. Prezzo di partenza 12,50 € al mese o
  125 € all'anno, IVA esclusa, modificabile in /admin › Piani › Lingue
  extra. Le redazioni in prova, con bonifico o di un rivenditore non la
  comprano online. L'API `/api/public/plans` restituisce anche `addons`,
  per la pagina prezzi del sito. Migrazione 0045 (SQLite 0034).
- **IVA sui pagamenti con la carta** (Francesco, 10 novembre). Finora Mollie
  incassava il prezzo di listino, che è IVA esclusa. Ora a piani, rinnovi,
  pacchetti di crediti e lingue extra si aggiunge l'IVA al 22%; niente IVA
  per un'azienda con partita IVA in un altro paese dell'UE (inversione
  contabile). Ogni pagamento tiene la sua IVA (`payments.vat_cents`), che
  la console mostra in Pagamenti («di cui IVA»). I rinnovi seguono il
  cliente: se cambiano i dati di fatturazione l'importo a Mollie cambia
  subito, e ogni ora il worker porta all'importo giusto gli abbonamenti
  fatti prima (anche quelli già attivi, `subscriptions.renewal_cents`).
  Le pagine del piano dicono quanta IVA si aggiunge. I bonifici registrati a
  mano restano come li scrive la console.

### Fixed

- **Sito e profilo › Lingue diceva ancora che ogni traduzione conta come
  un articolo del piano.** Dalla 0.33.0 non è così: un articolo uscito conta
  uno solo, in tutte le lingue scelte per il sito. Il testo ora lo dice,
  in tutte e 5 le lingue del pannello.

## 0.34.1

### Changed

- **La ricetta si corregge da «Modifica»** (Francesco, 10 novembre: nella
  ricetta dello sgombro non c'erano né ingredienti né procedimento da
  modificare). Il modulo di una ricetta propria ha ora la scheda:
  categoria, difficoltà, porzioni, tempi di preparazione e cottura,
  ingredienti (uno per riga, «200 g farina»), passaggi (uno per riga),
  tag. Salvando, la scheda pubblicata cambia con il testo.
- **Ricette scritte come un ricettario** (Francesco: «cuocere finché la
  carne non si stacca a scaglie»). La ricetta è scritta nella lingua del
  sito con le parole di cucina di quella lingua, mai tradotte alla
  lettera; quando un passaggio è finito lo dice da ciò che il cuoco vede,
  tocca o misura (il pesce diventa opaco e si sfalda con la forchetta, la
  temperatura al cuore quando conta). L'apertura è di una-tre frasi senza
  titoli, solo sul piatto: niente consigli di salute, dieta o sport; se il
  modello scrive un saggio attorno alla ricetta, la bozza torna indietro.

## 0.34.0

### Added

- **Menù interi per i siti di ricette** (Francesco, 10 novembre). Su un
  tema delle feste la redazione può proporre un menù («menù» tra i
  formati). Il modello sceglie 3-5 portate: per ognuna riusa una ricetta
  che il sito ha già, quando va bene, e scrive una ricetta nuova solo per
  le portate che mancano. Ogni ricetta nuova è una bozza a sé (ricetta
  propria, con la sua foto e la sua categoria) legata al menù. Il menù è
  un articolo (sui siti come lagrandecucina.it va in Tecnica) con
  un'apertura e una sezione per portata con il link alla ricetta. Nella
  bozza del menù si vedono le portate, quali ricette sono già sul sito e lo
  stato delle nuove. «Approva e pubblica» sul menù approva e pubblica prima
  le ricette nuove; il menù parte da solo appena l'ultima è online, con i
  loro indirizzi veri. La redazione automatica non scrive menù: solo
  ricette.

### Changed

- **Ricette da più fonti** (Francesco, 10 novembre: le proposte del giorno
  venivano tutte da Gustoblog, il feed i cui titoli somigliano di più ai temi
  del profilo). Una stessa testata dà al massimo un terzo delle notizie che
  il modello legge e delle proposte del giorno (con una o due proposte nessun
  limite). Vale per tutti i siti.
- **Siti di ricette: ricette di stagione e delle feste in anticipo.** Un sito
  riconosciuto come sito di ricette parte in modalità mista: metà delle
  proposte sui temi del mese e sulle lacune del sito, metà su quello che piace
  adesso. Ogni tema diventa una ricetta propria. I temi del mese guardano
  avanti: a novembre il Natale, un mese prima la Pasqua. Per un sito già
  collegato basta scegliere «Misto» in Sito e profilo › Tipo di contenuti.

## 0.33.8

### Security

- **Nessuno ti tiene più fuori dal pannello sbagliando la tua password.**
  Prima, dopo 5 password sbagliate, si bloccava l'account: chi conosceva
  la tua email poteva lasciarti fuori ripetendo tentativi. Ora i tentativi
  si contano per connessione: dopo 5 password sbagliate per la stessa
  email, o 20 in tutto, da quella connessione si riprova dopo 15 minuti,
  mentre tu dalla tua entri. L'account si blocca solo dopo 50 errori da
  connessioni diverse. Vale anche per «Password dimenticata» (punto B1
  dell'audit di ottobre, ripreso nell'audit del 10 novembre).

### Changed

- **Il pannello ha i suoi caratteri.** IBM Plex Sans per il testo, IBM
  Plex Mono per i numeri e Source Serif 4 per gli articoli nella bozza
  ora vengono serviti dal pannello (168 KB, in cache per un anno). Prima
  ognuno vedeva il carattere del suo sistema: Segoe su Windows, San
  Francisco su Mac. Licenza SIL Open Font in
  `editor-mode/src/editor/static/FONTS-OFL.txt`.

## 0.33.7

### Security

Correzioni dell'audit del 10 novembre (sicurezza, pipeline e stile del
pannello).

- **Niente link inventati negli articoli.** Il modello poteva mettere un
  link suo dentro una frase (anche chiesto da una pagina letta tra le
  fonti) e con la redazione automatica usciva sul sito. Ora nelle frasi,
  nel titolo e nel sommario restano solo i link alle fonti citate; gli
  altri diventano testo. I link agli altri articoli del sito restano in
  «Leggi anche». I prompt dicono che il testo delle fonti è materiale, non
  istruzioni.
- **Cambi di redazione o di sito solo dal pannello.** «Entra come
  direttore», il cambio di redazione e il cambio di sito aperti da un link
  di un altro sito portano alla home invece di cambiare la sessione.
- L'API rifiuta PUT e DELETE con il cookie da un'altra origine, come già
  faceva per POST.
- Le copertine escono sempre con il tipo letto dall'immagine, non con
  quello dichiarato dal servizio che le disegna.
- Una sitemap che dichiara un DOCTYPE o delle entità si salta invece di
  leggerla.
- «Password dimenticata» scrive nel log perché l'email non è partita.

### Fixed

- **Punto della Sera:** cercava l'articolo del sito anche tra le copie
  dell'archivio comune (una ricerca senza collegamento alle fonti), e in
  quel caso non lo registrava tra le notizie della redazione.
- **Impostazioni del sito:** fermare la redazione automatica nello stesso
  istante in cui il giro orario salva il conteggio del giorno poteva far
  perdere una delle due scritture; ora la seconda aspetta la prima.

### Changed

- **La ricetta si rilegge com'è sul sito** (Francesco, 10 novembre: nella
  bozza vedeva solo titolo e apertura). La pagina della bozza mostra in
  cima la copertina e, per le ricette proprie di un sito di ricette, la
  scheda completa: categoria, difficoltà, porzioni, tempi, calorie e valori
  per porzione, ingredienti con le quantità, passaggi numerati ed etichette.
- **Meno passaggi per pubblicare.** Il direttore ha «Approva e pubblica»
  sulla bozza da leggere: un clic approva e manda l'articolo alla
  destinazione del sito (se ce n'è più d'una la sceglie accanto al
  bottone). Su una bozza già approvata il suo bottone è «Pubblica» e parte
  subito, senza passare da Pubblicazioni ad approvare la propria
  richiesta. Il modo predefinito è «Pubblica subito». Chi non è direttore
  continua a chiedere la pubblicazione.
- **Le ricette proprie non parlano delle fonti.** L'apertura racconta il
  piatto e non nomina articoli, blog o siti letti per l'idea (prima poteva
  uscire «Gustoblog lo ha rimesso al centro»). I controlli per Google non
  chiedono più a una ricetta propria due fonti citate e due sottotitoli:
  ha già Ingredienti e Procedimento.
- Desk: in «Chiedi un articolo» la spiegazione sta sotto il pulsante; la
  tabella Siti al telefono diventa a schede (prima si vedevano 4 colonne
  su 9); «1 chiamata» invece di «1 chiamate».
- Console: il margine negativo è ambra, non rosso (rosso solo per gli
  errori).
- Tolti dal foglio di stile blocchi mai usati.
- **Collega il sito mostra la cartella delle ricette.** Su un sito di
  ricette collegato via GitHub la pagina diceva solo «Cartella degli
  articoli» (per esempio content/tecnica) e sembrava che le ricette
  finissero lì. Ora c'è anche la riga «Cartella delle ricette» con la
  cartella trovata (content/ricette) e le categorie lette dalle ricette
  (I Primi, I Secondi, I Dessert…): le ricette di Redazia vanno lì, le
  altre pagine nella cartella degli articoli.

## 0.33.6

### Changed

- **Console › Consumi: "Modello per passo" e "Prezzi di riferimento" in
  ordine** (Francesco, 9 novembre: "che brutti sono questi 2 blocchi").
  I passi sono righe allineate, nome a sinistra e modello a destra, in tre
  colonne su schermo largo e una sul telefono, con un solo "Salva i
  modelli" in fondo. I prezzi sono una tabella compatta (modello, entrata
  €/M, uscita €/M, nota, Togli) con una riga vuota per un altro modello e
  un solo "Salva i prezzi": niente più etichette ripetute per ogni modello
  né un pulsante per riga. Entrata e uscita vuote lasciano il modello senza
  prezzo; un numero sbagliato non ferma gli altri e il messaggio dice quale
  modello non è stato salvato.

## 0.33.5

### Fixed

- **Aggiornamento da riprovare dopo una versione ripubblicata** (Francesco,
  9 novembre). La 0.33.4 era stata taggata sul commit della 0.33.3: il
  server, aggiornato, rispondeva ancora 0.33.3 e Console › Aggiornamenti
  toglieva il pulsante dicendo di aspettare la versione successiva, anche
  dopo che la release era stata rifatta sul commit giusto. Ora il messaggio
  resta, con il pulsante "Riprova l'aggiornamento alla …": il server
  scarica di nuovo le immagini.

## 0.33.4

### Added

- **Siti di ricette: Redazia lo capisce da sola e scrive ricette sue**
  (Francesco, 8 novembre, sul sito lagrandecucina.it). Un sito è di
  ricette quando il suo repository ha una cartella di ricette (file con
  ingredienti e passaggi, trovata collegando GitHub) oppure quando il
  profilo dice che pubblica ricette. Allora:
  - le notizie fanno solo da spunto (il piatto, l'ingrediente, la stagione
    di cui si parla): la ricetta è del sito, con porzioni, tempi,
    difficoltà, categoria, tag (vegetariano, keto, veloce…), ingredienti
    con quantità e unità, passaggi e valori nutrizionali stimati; solo
    l'apertura, sul perché del piatto adesso, cita le notizie;
  - la categoria è una delle rubriche del sito (le portate lette dalle
    ricette, I Primi, I Secondi…) o una nuova nello stesso stile, che il
    sito crea;
  - la copertina è la foto del piatto finito, come la farebbe un
    fotografo di cucina;
  - sulle pagine del sito non ci sono fonti: restano in Redazia.
  Le ricette scritte da Redazia non vengono tradotte con la loro scheda:
  per ora un sito di ricette va tenuto in una lingua.
- **Siti che tengono gli articoli in JSON** (un sito Next.js con
  `content/*.json`): la destinazione GitHub scrive il nuovo articolo come
  file JSON con la forma di quelli già presenti (chiavi e ordine, numeri
  che restano numeri, ingredienti `{quantity, unit, name}`, passaggi
  `{description}`, valori comuni come l'autore). Le ricette vanno da sole
  nella cartella delle ricette (`recipe_path`, trovata collegando il sito o
  accanto alla cartella degli articoli), con l'immagine accanto a quelle
  delle altre ricette e il loro indirizzo; il resto nella cartella degli
  articoli. Collegando GitHub senza indicare la cartella si sceglie quella
  con più articoli, guardando anche dentro `content/`.
- **Fonti negli articoli pubblicati: "Nessuna"**, in Sito e profilo, per
  ogni sito: senza numeri di nota e senza elenco in fondo.

## 0.33.3

### Added

- **Accessi compresi nel piano** (Francesco, 8 novembre). Ogni piano dice
  quante persone può avere una redazione, il direttore compreso e gli
  inviti ancora aperti contati. Vuoto vuol dire senza limite. Valori di
  partenza: Solo 1, Starter 2, Pro 5, Business 10, Agency e gli altri
  senza limite; le redazioni prendono quelli del loro piano (migrazione
  0044, SQLite 0033). Oltre il limite il direttore non può invitare altri,
  ma chi c'è già resta. Si cambia in Console › Piani e crediti e, per una
  singola redazione, in "Nome e limiti". In Impostazioni › Persone della
  redazione si vede "usati su N"; `/api/public/plans` ha `max_users`, che
  redazia.com mostra nella scheda del piano.

## 0.33.2

### Changed

- **Console › Aggiornamenti: solo gli ultimi cinque** (Francesco, 8
  novembre). Lo storico mostrava fino a 30 aggiornamenti in una tabella
  sempre più lunga, e la barra di un aggiornamento in corso finiva in
  fondo alla pagina, sotto la tabella. Ora la barra sta subito sotto lo
  stato; sotto ci sono gli ultimi cinque aggiornamenti e i precedenti
  restano chiusi in "Aggiornamenti precedenti".
- **Piani e pacchetti di crediti in ordine di prezzo** (Francesco, 8
  novembre), dal più economico, nella console e in `/api/public/plans`,
  quindi anche su redazia.com. I piani senza prezzo vanno in fondo.

### Fixed

- **Un piano nuovo con il nome di uno rinominato.** Un piano rinominato
  tiene il suo codice: creandone un altro con il vecchio nome, la console
  rispondeva "il piano esiste già". Ora il nuovo piano prende il primo
  codice libero (`solo-2`). Se il codice lo scrivi tu ed è già usato, la
  console dice di quale piano è.

## 0.33.1

### Changed

- **Listino dei crediti alzato** (Francesco, 8 novembre). Un credito vale
  circa un centesimo (pacchetto da 500 a 5 €). Un articolo oltre il piano
  costava 25 crediti, circa 0,25 €: meno di un articolo di qualunque piano.
  Ora costa 100 crediti, circa 1 €. Una copertina rifatta dopo le due
  gratuite passa da 3 a 5 crediti. Una traduzione in una lingua che il sito
  non ha resta a 10 crediti.

  La migrazione 0043 (SQLite 0032) cambia solo i prezzi rimasti quelli di
  prima: un prezzo cambiato a mano in Console › Piani e crediti › Listino
  resta com'è.

## 0.33.0

### Changed

- **Un articolo del piano è un articolo che esce, in tutte le lingue del
  sito** (Francesco, 7 novembre). Prima "articoli al mese" contava ogni
  bozza: traduzioni, riscritture del caporedattore AI e articoli scartati.
  Con 1 sito, 2 lingue e 30 articoli si avevano 15 articoli più 15
  traduzioni, meno le riscritture. Ora:
  - un articolo conta una volta per proposta, comprese le sue riscritture
    e versioni (`quotas.articles`, `drafts.written`);
  - le traduzioni nelle lingue del sito sono comprese nell'articolo; una
    traduzione in un'altra lingua si paga con i crediti (10);
  - gli articoli scartati dalla redazione automatica non contano;
  - una bozza scartata da una persona conta, perché è stata scritta e
    letta.

  Con 30 articoli e 2 lingue si hanno quindi 30 articoli, ognuno anche
  tradotto.
- **Guida › "Cosa contano i limiti del piano".** Spiega con esempi:
  - articoli e lingue;
  - limiti per redazione e non per sito (siti, articoli, fonti, crediti);
  - lingue e articoli al giorno scelti per sito;
  - a cosa servono i crediti, con i prezzi del listino.

## 0.32.2

### Fixed

- **La redazione automatica si fermava a 10 articoli su 12 ogni giorno**
  (Francesco, 7 novembre, aitoday.it). Gli articoli anticipati dal giorno
  dopo si contavano quando la proposta veniva accettata, non quando
  l'articolo usciva. Un anticipato scartato (per esempio un doppione) o mai
  scritto esauriva le anticipazioni del giorno ed era sottratto anche al
  giorno dopo, e il giro si ripeteva. Ora:
  - si anticipa finché gli articoli usciti oltre il tetto sono meno di
    quelli permessi, con al massimo due tentativi ciascuno;
  - il giorno dopo si sottraggono solo gli articoli usciti davvero oltre il
    tetto.

  Il Desk spiega il tetto ridotto: "10 su 12: 2 li aveva anticipati ieri".
  Il messaggio sulle notizie rimaste fuori dal tetto compare solo quando le
  anticipazioni sono davvero finite.

## 0.32.1

### Added

- **Formato ricetta.** Quando una fonte seguita dal sito dà una ricetta
  intera (ingredienti e passaggi), la proposta può avere il formato
  "ricetta". La bozza ha una breve introduzione e due sezioni nella lingua
  del sito, "Ingredienti" (elenco puntato) e "Procedimento" (elenco
  numerato). Ogni ingrediente e ogni passaggio ha la sua fonte come le
  altre frasi. Porzioni e tempi restano solo se le fonti citate li danno:
  un numero che le fonti non hanno non si pubblica.
- **Scheda `Recipe` per Google.** Il JSON-LD dell'articolo ha anche
  `Recipe` di schema.org (ingredienti, passaggi `HowToStep`, porzioni,
  tempi in ISO 8601). La scheda arriva:
  - nel front matter delle destinazioni GitHub, come `recipe`;
  - nel webhook e nel JSON Feed, come campo `recipe`;
  - nella pagina scaricabile, con la copertina come immagine.

  Le correzioni del caporedattore e le traduzioni la tengono, con i titoli
  tradotti. Il modello pubblico redazia-site-template la trasforma in
  `Recipe` sulla pagina dell'articolo. La guida
  `editor-mode/docs/template-su-misura.md` spiega cosa aggiungere a un
  template su misura.

### Changed

- I formati "confronto" e "dati" hanno ora il loro nome nella pagina delle
  proposte, in tutte le lingue.

## 0.32.0

### Added

- **Contenuti senza scadenza** per i siti che non vivono di notizie del
  giorno, come ricette, lifestyle e blog aziendali (Francesco, 7 novembre:
  "la metrica non è solo l'urgenza della notizia, ma la pertinenza
  tematica, la stagionalità o la copertura di un cluster"). In Sito e
  profilo › "Tipo di contenuti" si sceglie tra:
  - notizie del giorno, come prima;
  - contenuti senza scadenza;
  - misto, con la percentuale di contenuti senza scadenza (50% di base).

  Un tema nasce da una di tre ragioni (`editor/evergreen.py`):
  - un argomento del profilo su cui il sito ha scritto poco o niente, o
    niente da più di quattro mesi;
  - una ricerca Google che porta visite al sito (Search Console) e a cui
    nessun articolo del sito risponde;
  - un tema del mese, come stagione, feste o calendario del settore,
    chiesto al modello una volta al mese e salvato nel profilo.

  La regola resta la stessa: l'articolo si scrive dalle fonti seguite
  dal sito negli ultimi 30 giorni, e ogni frase ne cita una. Un tema senza
  almeno due fonti di due testate non viene proposto, e un tema proposto
  non torna per tre settimane. La proposta spiega perché il sito ne ha
  bisogno ("Perché questo tema") al posto di "Perché oggi", e non mostra
  l'hype. La bozza è una guida che mette insieme più fonti, senza la
  sezione di analisi pensata per le notizie. Il caporedattore AI non la
  rifiuta più perché "unisce più fatti", ma la rifiuta se inventa legami
  tra le fonti. Lo stesso vale per gli articoli chiesti dalla redazione.

### Changed

- **Riserve degli editori: contano gli assistenti AI che leggono, non i
  crawler di addestramento** (Francesco, 7 novembre). Con la 0.31.7 bastava
  che il `robots.txt` chiudesse la pagina a un qualsiasi crawler AI: ce l'ha
  il 62% delle fonti comuni, e quasi tutti gli articoli si sarebbero scritti
  dal solo sommario. Ora la pagina è riservata se il `robots.txt` la chiude
  a uno degli agenti con cui gli assistenti AI leggono il web per rispondere
  agli utenti (ChatGPT-User, OAI-SearchBot, PerplexityBot, Perplexity-User,
  Claude-User, Claude-SearchBot, MistralAI-User, DuckAssistBot), oppure se
  la riservano `tdmrep.json` o `tdm-reservation`, come prima. Il blocco dei
  soli GPTBot, CCBot, Google-Extended e simili non conta più. Le fonti
  comuni riservate scendono a circa il 46%.

- **Menu laterale più chiaro**, nella console `/admin` e nella redazione
  (Francesco, 7 novembre: "questa barra fa un po' cagare"):
  - un'icona per ogni voce;
  - la voce aperta in azzurro tenue con una riga a sinistra, non più nera;
  - in fondo una scheda con l'iniziale, l'utente, il ruolo e il pulsante per
    uscire;
  - la versione installata sotto la scheda.

### Fixed

- **"Meglio aggiornare un tuo articolo" compariva su ogni proposta** in
  Trend e piano, con il link vuoto. La pagina leggeva il metodo `update`
  del dizionario invece del valore. Ora compare solo quando il sito ha
  davvero un articolo molto simile.

## 0.31.7

### Changed

- **Rispetto delle riserve degli editori contro l'AI** (Francesco,
  7 novembre; art. 70-quater della legge sul diritto d'autore e art. 4
  della direttiva 2019/790). Una pagina è riservata se:
  - il suo `robots.txt` chiude la pagina a uno dei crawler AI noti
    (GPTBot, ChatGPT-User, OAI-SearchBot, CCBot, Google-Extended,
    ClaudeBot, anthropic-ai, PerplexityBot, Applebot-Extended,
    Meta-ExternalAgent, Bytespider e altri), anche se è aperta a tutti
    gli altri;
  - `/.well-known/tdmrep.json` la riserva (`tdm-reservation: 1`);
  - la pagina porta l'intestazione o il meta tag `tdm-reservation: 1`.

  Una pagina riservata non viene letta: l'articolo si scrive dal sommario
  che il feed dell'editore distribuisce, come già succede per le pagine
  chiuse dal `robots.txt`. Vale per le notizie, per i documenti originali
  citati, per le fonti primarie e per i link delle richieste. La ricerca
  delle fonti non cambia, perché legge solo i feed.

## 0.31.6

### Added

- **Guida per chi costruisce il proprio template**
  (`editor-mode/docs/template-su-misura.md`, Francesco, 7 novembre). Copre
  i tre canali (GitHub, webhook, feed) con tutto quello che serve:
  - struttura del repository, campi del front matter e nomi riconosciuti;
  - cosa deve fare il template;
  - come Redazia pubblica, modifica e ritira;
  - firma del webhook con esempi in Node, PHP e Python, risposta attesa;
  - verifiche prima di andare in produzione.

### Changed

- **Webhook più completo.** La pubblicazione e la modifica mandano anche
  `translation_of`, cioè lo slug dell'originale, perché il sito possa
  collegare le lingue. La pubblicazione manda anche `date`. Se il sito
  risponde con `{"id": ..., "url": "https://..."}`, Redazia ricorda
  l'indirizzo vero dell'articolo e lo mostra nella bozza e nelle
  pubblicazioni.

## 0.31.5

### Changed

- **Meno notizie americane** (Francesco, 7 novembre: dal 4 novembre circa
  metà degli articoli di aitoday.it parlava degli Stati Uniti).
  - **Fonti bilanciate per paese:** ogni feed della base comune dice il suo
    paese. Nelle sezioni mondo ed economia nessun paese straniero supera i
    6-7 feed: quelli americani scendono da 19 a 6. Arrivano testate di 48
    paesi, almeno due per 27 di loro, in inglese o nelle 5 lingue (una
    testata in polacco non si collegherebbe alle altre sulla stessa
    notizia), e 14 nuove fonti in italiano: 62 feed nuovi in tutto. Le fonti
    tecnologiche restano come sono, in gran parte americane.
  - **Tolti 7 feed fermi da mesi:** Corriere della Sera (fermo dal 2024),
    CNN World, Wall Street Journal, Xinhua, The Telegraph, ANSA Tecnologia,
    Wired Italia.
  - **L'hype conta i paesi, non le testate:** venti testate americane sulla
    stessa notizia valgono un paese, mentre una italiana, una francese e una
    tedesca ne valgono tre (formula `hype-v2`). Lo stesso vale per il
    punteggio delle proposte. Il minimo di testate della redazione
    automatica, che serve a confermare il fatto, conta ancora le testate.
  - **Massimo per paese straniero:** ogni proposta riceve il paese della
    notizia. La redazione automatica dà a un paese straniero al massimo un
    terzo degli articoli del giorno, poi aspetta il giorno dopo. Il paese
    del sito non ha limite. Si regola in Sito e profilo › Redazione
    automatica (Massimo per paese straniero, Paese del sito; 0 = nessun
    limite).
  - **Rubriche bilanciate** (aitoday.it, primi 87 articoli: Mondo 32,
    Economia 18, Clima 1). Ogni proposta riceve anche la rubrica del sito a
    cui appartiene. Una rubrica non supera un quarto degli articoli del
    giorno, regolabile in Massimo per rubrica (%). Quando un paese o una
    rubrica sono pieni, la redazione lo dice a chi prepara le nuove
    proposte, che cerca altrove.

## 0.31.4

### Fixed

- **Il Punto della Sera non va più perso** (aitoday.it, 6 novembre: scartato
  perché titolo e sommario presentavano come fatti i legami tra le notizie).
  Il direttore AI non mette più legami, cause ed effetti nel titolo e nel
  sommario: restano nel testo, come lettura. Se il caporedattore lo boccia
  comunque, il Punto della Sera prende titolo e sommario neutri (per esempio
  «Il Punto della Sera del 6 novembre») con il testo invariato, e viene
  riletto. Se non passa nemmeno così e dopo una riscrittura, aspetta il
  direttore come bozza: non viene mai scartato.

## 0.31.3

### Fixed

- **Dopo un salvataggio la pagina resta dov'era** (Francesco, 6 novembre:
  salvando Breaking news si finiva in cima al profilo, salvando il Punto della
  Sera in cima alla pagina). Il modulo salvato torna alla stessa altezza
  sullo schermo, con il suo riquadro aperto e il messaggio dentro, e
  Breaking news resta nella scheda Redazione automatica.

## 0.31.2

### Fixed

- **La stessa notizia il giorno dopo** (aitoday.it, 5-6 novembre: Tinto
  Brass, Vance e Hegseth, il congresso Sirm raccontati di nuovo con un altro
  titolo e un'altra copertina). Un titolo con tre nomi in comune con un
  articolo dei due giorni prima è la stessa notizia. Il caporedattore AI
  riceve i titoli del sito degli ultimi due giorni e scarta, senza
  riscriverla, una notizia già data che non porta fatti nuovi.
- **Meno articoli pagati e poi scartati** (5 novembre: 7 su 28, circa 0,20 $
  l'uno, quasi sempre per dettagli che non stanno nella fonte letta). Se
  della notizia si leggono meno di 150 parole l'articolo non si scrive e la
  proposta lascia il posto a un'altra, senza spesa. Il redattore non scrive
  ciò che sa a memoria (carriere, record, date). Il caporedattore taglia fino
  a sei frasi senza fonte invece di far riscrivere tutto.

## 0.31.1

### Added

- **Breaking news** (Sito e profilo › Come scrive il sito, vicino al Punto
  della Sera, spenta in partenza). Con la redazione automatica accesa, una
  notizia che almeno N testate diverse (3 in partenza, da 2 a 10) danno entro
  poche ore (3 in partenza, da 1 a 12) esce anche quando gli articoli del
  giorno sono finiti o fuori orario, dopo il caporedattore AI come le altre.
  Al massimo 3 al giorno, e ognuna è un articolo in meno il giorno dopo.

### Fixed

- **La stessa notizia non esce più due volte** (aitoday.it, 6 novembre: il
  governo che impugna la legge veneta sul fine vita, pubblicato alle 7:04 e
  alle 7:06 con due titoli). Le due proposte avevano titoli quasi uguali ma
  angoli più lunghi e diversi, e il confronto sulle parole dell'angolo le
  lasciava passare insieme nello stesso giro. Ora la redazione automatica
  confronta anche i soli titoli, sia quando sceglie le proposte sia prima del
  caporedattore: un articolo che racconta una notizia già approvata nei due
  giorni prima viene scartato prima di pagare caporedattore e copertina.

## 0.31.0

### Added

- **Chiedi un articolo, dal Desk.** Un riquadro dove scrivere cosa si vuole
  (argomento, taglio, a chi è rivolto) e incollare fino a 5 link facoltativi.
  La redazione legge i link, cerca nell'archivio delle ultime due settimane
  le notizie sull'argomento tra le fonti del sito (anche quelle in inglese:
  il modello dà le parole da cercare nelle due lingue) e scrive la bozza con
  i soliti controlli: ogni frase cita una fonte, niente fatti che le fonti
  non danno. Se non trova niente da citare non scrive e lo dice in
  Attività, con i link che non si sono potuti leggere. Conta come un
  articolo del piano; la bozza si rilegge, si approva e si pubblica come le
  altre.
- **Le rubriche come categorie e menu del sito WordPress, subito.** Appena
  si confermano il contesto e le rubriche (Primi passi, o «Crea il profilo
  dal contesto»), e ogni volta che le rubriche cambiano in Redazione
  automatica › Regole, Redazia crea su WordPress le categorie che mancano e
  mette una voce per ciascuna nel menu principale del tema, creando il menu
  se non c'è; le voci già presenti restano come sono. Per un sito già
  collegato c'è il bottone «Crea categorie e menu su WordPress» in Regole.
  Le categorie le crea anche un utente editore; i menu su WordPress li
  cambia solo un amministratore, e con un editore Attività lo dice.
- **Rubrica scelta anche per le bozze scritte a mano.** Lo scrittore indica
  la rubrica del sito a cui appartiene l'articolo (prima la sceglieva solo il
  caporedattore della redazione automatica), e nella pagina della bozza la
  si può cambiare prima di chiedere la pubblicazione, o per un articolo già
  su WordPress insieme a «Aggiorna sul sito».

### Fixed

- **WordPress: il titolo non compare più due volte** (wp.aitoday.it, 5
  novembre). Il titolo va nel campo di WordPress, che il tema mostra; il
  testo non lo ripete più in testa.
- **WordPress: l'articolo va nella categoria della sua rubrica** invece di
  "Uncategorized": la bozza pubblicata a mano non aveva una rubrica (vedi
  sopra), e ora anche l'aggiornamento di un articolo già online crea la
  categoria che manca.

## 0.30.5

### Fixed

- **Le traduzioni inglesi non vengono più scartate per una frase copiata**
  (aitoday.it, 5 novembre: l'italiano approvato, l'inglese scartato per "1-2
  frasi da rivedere"). Una notizia da fonti inglesi, tradotta in inglese,
  riprendeva alla lettera qualche frase della fonte; la frase risultava
  copiata e la traduzione intera finiva scartata. Ora quelle frasi vengono
  riformulate sul posto, con gli stessi numeri, nomi e fonti, e la
  traduzione esce.
- **Il caporedattore AI legge le fonti quanto lo scrittore.** Leggeva 2.500
  caratteri di ogni fonte, lo scrittore fino a 6.000: per gli articoli da
  arXiv scartava come "dettagli che non compaiono nella fonte" cose scritte
  più avanti nel paper. Ora legge 6.000 caratteri anche lui.

## 0.30.0

### Added

- **Il Punto della Sera.** Ogni sera, all'ora scelta (21 di default, nel
  fuso della redazione), un direttore AI legge tutti gli articoli pubblicati
  dal sito nelle 24 ore. Sceglie il fatto che pesa di più, cioè l'agenda
  setting: per le conseguenze, non per quanti articoli ha avuto. Scrive
  un'analisi che apre con quel fatto e ci ruota intorno, collega le altre
  notizie e lascia sport e cronaca minore a un breve "In breve" finale. Il
  tono è da Sole 24 Ore o Economist. Ogni frase cita gli articoli del giorno,
  che diventano i link dell'analisi. Il caporedattore AI la rilegge con
  regole sue: un collegamento che nessun articolo afferma va scritto come
  lettura della redazione, mai come notizia, e non entra nessun fatto
  nuovo. Esce nella rubrica "Analisi e Approfondimenti" (su WordPress la
  crea Redazia), con copertina e traduzioni. Con la redazione automatica
  accesa esce da solo, altrimenti resta tra le bozze. Parte solo se nella
  giornata sono usciti abbastanza articoli (6 di default). Si imposta in
  Sito e profilo › Redazione automatica › Come scrive il sito. È compreso
  nei piani Pro, Business e Agency (/admin › Piani › Punto della Sera;
  migrazione 0042).
- **Articoli più critici.** In fondo all'articolo una sezione breve ("Cosa
  cambia", "Perché conta") collega i fatti tra loro e con le notizie di
  contesto, e li pesa: chi lo dice e che interesse ha, promesse senza data,
  numeri senza confronto, fonti in disaccordo. Si regge solo sui fatti delle
  fonti. Una causa, un effetto o una previsione che nessuna fonte afferma
  si scrive come lettura, mai come notizia, e il caporedattore AI ha una
  regola per toglierla. Si spegne per sito ("Analisi negli articoli").

### Fixed

- **Le formule da testo scritto da una macchina.** Oltre al divieto nel
  prompt, ora un controllo a regole in cinque lingue trova le formule
  ("non si tratta solo di", "in un contesto in cui", "rappresenta un passo",
  "gioca un ruolo chiave", "resta da vedere", "in conclusione",
  "underscores the importance", "marks a turning point"...). Le frasi che
  le contengono vengono riformulate sul posto, con gli stessi numeri e nomi
  e le stesse fonti; se la riformulazione cambia qualcosa, la frase resta
  com'era. Il controllo è gratuito: il modello lavora solo sulle frasi
  colpite. La lista si allunga in `editor/style.py`.
- **La rubrica non si perde più dopo una correzione.** Una correzione frase
  per frase nella pagina della bozza toglieva la rubrica dall'articolo, e
  «Aggiorna sul sito» lo mandava senza.

## 0.29.0

### Fixed

- **La redazione automatica non si ferma più a metà giornata** (Francesco:
  20 articoli al giorno, 9 pubblicati fino alle 13, poi più niente). Il sito
  chiedeva almeno 2 testate sullo stesso fatto, ma dalle 13 tutte le 167
  proposte risultavano da una sola testata, quindi ogni ora non ne passava
  nessuna. Ora le testate sono una preferenza, non un blocco ("se non c'è la
  seconda testata va fatto comunque"): passano prima le notizie confermate
  da più testate, poi le migliori delle altre, e il caporedattore AI le
  rilegge come tutte. L'articolo cita tante testate quante la notizia ne ha.
  Il giro legge fino a 250 proposte, non più le prime 80. Il campo in Sito e
  profilo si chiama "Testate sullo stesso fatto".
- **Lo stesso fatto in qualsiasi lingua.** Il controllo confrontava le
  parole dei titoli: un titolo italiano non trovava mai la stessa notizia
  raccontata in inglese da BBC o Guardian. Adesso le notizie che hanno in
  comune nomi e numeri vengono mostrate al modello, che con una chiamata per
  giro conferma quali raccontano lo stesso fatto. Ogni proposta viene
  controllata una volta sola.
- **Hacker News e Reddit.** Entrano nella base comune le storie di HN sopra
  i 100 punti e i post più votati del giorno di tre gruppi di subreddit
  (mondo, tecnologia e AI, scienza ed economia), letti con una richiesta per
  gruppo perché Reddit limita le richieste. Nella ricerca delle fonti, HN e
  Reddit non vengono più scartati perché "ripetono" notizie già note: è
  proprio l'hype che misurano.
- **WordPress senza permalink "belli".** Dove `/wp-json/` risponde 404
  (permalink semplici, o un server che non legge il `.htaccess`, come
  wp.aitoday.it), il collegamento e la pubblicazione passano da soli a
  `?rest_route=`. Prima il controllo dell'utente falliva con "WordPress ha
  risposto con un errore (404)". Sotto "Utente WordPress" la pagina dice che
  serve lo username di wp-admin, non il nome dato alla password, e il
  messaggio di accesso rifiutato indica la riga del `.htaccess` per i server
  che non passano l'accesso a WordPress.
- **Collegare WordPress, passo per passo.** La pagina del collegamento (e
  la guida per i clienti) va in quattro passi: permalink «Nome articolo»,
  un utente solo per Redazia con ruolo Editore (perché è più sicuro
  dell'amministratore e cosa può fare), la password applicativa con i nomi
  esatti dei menu di WordPress, la prova con lo username giusto e la riga
  del `.htaccess` se l'accesso viene rifiutato.
- **Sito WordPress nuovo, senza articoli.** Il collegamento conta gli
  articoli già pubblicati: se non ce n'è nessuno l'analisi non parte (non
  troverebbe nulla) e la pagina porta a "Scrivi il contesto", da cui si crea
  il profilo.
- **Le categorie le crea Redazia.** Se su WordPress manca la categoria della
  rubrica di un articolo, alla pubblicazione Redazia la cerca per nome e, se
  non c'è, la crea. Non serve più prepararle prima.
- **Gli articoli persi vengono sostituiti.** Un articolo il cui lavoro è
  finito in errore prima della bozza, o una bozza rimasta a metà e mai
  approvata (con "tutto automatico"), non conta più tra quelli fatti: al
  giro dopo ne viene scritto un altro.
- **Niente più passaggi "in corso" da giorni.** Un passaggio interrotto da
  un riavvio si chiude da solo quando ne parte uno uguale.
- **Conservazione di 30 giorni anche nel compose del server.** Il valore
  predefinito era ancora 365. Su un server già installato aggiungere
  `EDITOR_RETENTION_DAYS=30` al file `.env`.

## 0.28.46

### Fixed

- **Meno frasi copiate parola per parola, senza falsarle** (Francesco: "perché
  non le cambia?" e "fai in modo che non falsi le frasi"). Quando una frase
  copia la fonte, al nuovo tentativo il modello riceve le parole esatte da
  dire con parole sue. Le frasi ancora copiate dopo i tre tentativi vengono
  riformulate al loro posto, e la nuova frase vale solo se ha gli stessi
  numeri, tutti i nomi, le stesse fonti, una lunghezza simile e non copia
  più: altrimenti resta la frase originale, segnalata da rileggere. Vale
  anche per la redazione automatica.

## 0.28.45

### Fixed

- **Si vede quale articolo sta scrivendo** (Francesco: "lo sta scrivendo ma
  non dice quale"). Il riquadro Attività mette il titolo accanto al lavoro
  ("Scrittura della bozza: «titolo» · sito"), anche per la redazione
  automatica, le traduzioni e le copertine, e l'avviso dopo "Scrivi" nomina
  la proposta accettata.

## 0.28.44

### Removed

- **Il limite di notizie lette al giorno**. L'archivio delle notizie è uno per
  tutto il server e legge ogni fonte una volta sola per tutte le redazioni,
  anche quelle aggiunte da una redazione: sul server il limite non fermava
  niente ed era solo un numero nel Desk. Tolto dai piani, dalla scheda della
  redazione, dalla nuova redazione, dall'API dei rivenditori (il campo
  `max_items_per_day` viene ignorato) e dal comando `tenant-plan`
  (`--max-items-day`). Il Desk mostra ancora quante notizie sono arrivate oggi
  dalle fonti seguite, come informazione. Il limite che conta resta il numero
  di fonti attive.

### Changed

- **L'archivio tiene le notizie 30 giorni invece di 365**
  (`EDITOR_RETENTION_DAYS`, `0` tiene tutto). Il sistema usa al massimo
  gli ultimi 14 giorni (doppioni della stessa notizia da altre testate);
  proposte, trend e temi guardano 2-7 giorni. Le notizie citate da una
  proposta o da un articolo restano per sempre, così ogni frase pubblicata
  conserva la sua fonte.

## 0.28.43

### Changed

- **Un articolo con motivi correggibili si corregge, non si butta**
  (Francesco: "sono motivi che si possono correggere, non credo sia tutta da
  buttare la notizia"). Quando il caporedattore AI lo ferma, prima di
  riscriverlo o scartarlo lo corregge dov'è: toglie o riformula solo le
  frasi indicate nei motivi, sistema titolo e sommario, e non aggiunge
  niente. Una "correzione" che porta frasi nuove non vale. L'articolo
  corretto ripassa tutti i controlli e il caporedattore; se ancora non va, lo
  riscrive il modello più forte come prima, e solo dopo viene scartato. Il
  riquadro Attività dice cosa è stato corretto. La correzione usa il modello
  scelto in Consumi per "Correzione degli articoli scartati".
- La riscrittura resta sulla notizia del titolo: niente argomenti, aziende o
  sezioni nuove che i motivi non chiedono (era successo con le azioni di
  Ørsted in un articolo sulle elezioni americane).

### Fixed

- **Il motivo dello scarto si legge nella bozza**: in cima alla pagina
  dell'articolo scartato o fermato ci sono i motivi interi, il link alla
  versione riscritta e come recuperarlo («Modifica», poi approvi tu). Nel Desk
  il motivo lungo finisce con "…" e "leggi tutto".

## 0.28.42

La console dell'amministratore rifatta come un vero gestionale, e la parte
del direttore sistemata (Francesco: "se voglio inserire una nuova redazione
e non esiste il cliente come faccio?").

### Added

- **Clienti inseriti a mano**: "+ Nuovo cliente" in Redazioni e in Clienti,
  con tutti i dati di fatturazione e il controllo dei doppioni (stessa email,
  partita IVA o codice fiscale: il modulo torna come l'avevi scritto, con il
  link al cliente che c'è già). Ogni cliente ha la sua scheda con i dati di
  fatturazione, le redazioni, i pagamenti e i rimborsi, "Modifica" ed
  "Elimina" (solo se non ha redazioni).
- **Nuova redazione in tre passi**: il cliente (nuovo con i suoi dati,
  esistente, oppure nessuno per una redazione interna), la redazione con il
  direttore (se l'email resta vuota è quella del cliente), il piano scelto
  da riquadri con prezzo e limiti. Non si crea niente finché tutto non è
  giusto, e un cliente che c'è già viene scelto da solo.
- **Pagamenti**: una pagina con tutti i pagamenti online, i bonifici, i
  crediti e i rimborsi, filtri per tipo, mese e testo, i numeri del mese
  (incassato, rimborsato, netto, non riusciti, ricorrente) e il CSV per il
  commercialista (punto e virgola e virgola decimale).
- **Una ricerca per tutta la console**, in cima a ogni pagina: redazioni,
  clienti e persone per nome, id, email, partita IVA, codice fiscale, città
  o telefono. Con un solo risultato si apre direttamente.
- **Le persone dalla console**: nella pagina della redazione si cambia il
  ruolo, si toglie una persona, si rimanda l'invito a chi non è ancora
  entrato (il vecchio link smette di valere).
- **"Registra un pagamento", "Cambia piano", "Ridai la prova", "Ferma i
  rinnovi"** nella pagina della redazione, ognuno nella sua finestra, con
  scritto prima cosa succede.
- **Lato direttore**: lo storico dei pagamenti (data, cosa, metodo, importo,
  stato) in Il mio piano e in Abbonamento, e "Rimanda" sugli inviti in
  Impostazioni.

### Changed

- **La console come gli altri gestionali**: menu a gruppi (Clienti, Vendite,
  Sistema) con "+ Nuova redazione" sempre in alto, un'intestazione con le
  azioni su ogni pagina, righe cliccabili e filtri a schede nelle tabelle.
  Redazioni si apre con i numeri del mese (incassato, ricorrente, in prova,
  da pagare, margine). La pagina della redazione è in due colonne (il mese,
  siti, pagamenti, persone; abbonamento, cliente, crediti, nome e limiti,
  sospendi o cancella) e le modifiche si fanno in finestre. Sul telefono ogni
  riga diventa una scheda compatta.
- **Dati di fatturazione in una riga** per il direttore, con il modulo dietro:
  aperto solo quando mancano.
- **"Come funziona questa pagina"** si apre solo alla prima visita di ogni
  pagina, poi resta come l'hai lasciato.

### Fixed

- **Copertina rifatta dopo la pubblicazione** (Francesco: "devo
  ripubblicarlo?"). La pagina dell'articolo avvisa che online c'è ancora
  quella vecchia e il riquadro Attività dice di premere «Aggiorna sul sito».
  Su WordPress l'aggiornamento ora manda anche la copertina nuova, solo se è
  cambiata (non la ricarica a ogni aggiornamento); sui webhook l'evento
  `update` porta la copertina attuale in `image`, come già diceva la guida.
  Su GitHub la copertina andava già.
- I messaggi dopo un pagamento, una prova ridata o i rinnovi fermati nominano
  la redazione, non il suo id.
- Le barre d'uso di Il mio piano e della console non escono più dal riquadro
  sul telefono.

## 0.28.41

### Changed

- **Copertine fotografiche** (Francesco: "un po' troppo cartoon"). Le due
  impostazioni, d'impatto ed editoriale, ora chiedono una vera foto di
  cronaca: una scena che un fotografo avrebbe potuto scattare (il luogo, le
  persone al lavoro, l'oggetto a terra o in laboratorio), non una veduta
  immaginata dell'evento come un satellite in fiamme nel cielo. Luce
  naturale, colori veri, fotocamera full frame con 35 o 50 mm, e il divieto
  esplicito di illustrazioni, render 3D, CGI, effetti luminosi e colori
  saturi. "D'impatto" resta fatta per farsi cliccare: un soggetto forte e
  vicino, contrasto naturale. Le copertine già pubblicate restano; si rifanno
  dalla bozza con "Rifai l'immagine".

## 0.28.40

Meno articoli pagati e poi buttati dalla redazione automatica.

### Changed

- **Le poche frasi sbagliate si tolgono, l'articolo resta** (Francesco: su 8
  notizie una sola approvata, 7 scritture pagate e non pubblicate). Le frasi
  che non superano i controlli (numeri che le fonti non hanno, passaggi
  copiati, frasi senza fonte) e i titoletti che parlano delle fonti ("Cosa è
  confermato") si tolgono senza chiamare il modello, se sono poche (al più
  tre, un quarto dell'articolo) e non nell'apertura. Il caporedattore AI può
  chiedere di togliere fino a tre frasi invece di rifiutare l'articolo; rifiuta
  solo quello che i tagli non sistemano (notizia sbagliata, titolo o apertura
  sbagliati, due storie unite). Solo allora l'articolo si riscrive.
- **Lo scrittore non parla delle fonti**: niente frasi o titoletti su cosa è
  confermato, cosa non è chiaro, cosa dicono o non dicono le fonti. E cita da
  subito il numero di testate che la redazione automatica richiede, invece di
  scoprirlo dopo averlo pagato.
- **Scartata o Rifiutata**: un articolo tolto dalla redazione automatica è
  "Scartata", uno tolto da una persona "Rifiutata", in Bozze, nella bozza e
  nei filtri.
- **Oggi conta ogni articolo una volta**: le traduzioni pubblicate sono a
  parte ("più 11 traduzioni"), così il numero torna con quello della
  redazione automatica.

## 0.28.39

Secondo e terzo lotto dell'audit: la console di amministrazione e il pannello
della redazione.

### Changed

- **Redazioni in console**: schede per stato (Paganti, In prova, Ferme o
  sospese, Senza abbonamento) e ricerca; colonne "Articoli del mese" e
  "Margine nel mese" (prezzo del piano a listino meno il costo del modello);
  "Scheda" e "Entra come direttore" separati. La scheda della redazione ha un
  solo stato, il margine, i siti con profilo, redazione automatica e articoli
  di oggi, e i lavori falliti degli ultimi 7 giorni.
- **Clienti**: ricerca, schede per tipo, filtro per rivenditore, "Modifica"
  che apre la riga, link alle redazioni e ai pagamenti.
- **Piani**: tabella di confronto dei limiti, stato di Mollie in alto, nuovo
  piano dal nome (il codice è facoltativo e si ricava dal nome).
- **Rivenditori** con clienti, redazioni, quante in prova ed euro al mese a
  listino. **Amministratori** con ultimo accesso e "Sei tu"; il reset della
  password ha una sezione sua. **Aggiornamenti** con lo storico.
- **Consumi a schede**, una vista alla volta, con il costo per articolo
  contro il prezzo per articolo del piano.
- **Desk**: riquadro "Oggi" con pubblicati, scartati e cosa aspetta te, e se
  gli articoli del piano bastano al ritmo del mese; tolti i riquadri doppi.
- **Redazione automatica**: "Accendi con queste regole" in un clic, regole
  in tre gruppi (Quanti articoli, Quali notizie, Quando e dove) con le
  spiegazioni sotto ogni campo.
- **Bozze**: ricerca, schede per stato, filtro per lingua, traduzioni sotto
  l'originale. Nella bozza la pubblicazione sta in cima alla colonna; i
  controlli SEO dicono che non fermano la pubblicazione.
- **Pubblicazioni guidate dal tipo**: si sceglie prima dove escono gli
  articoli (WordPress, GitHub, webhook, Telegram), poi i campi e la
  spiegazione della credenziale di quel tipo; il nome della credenziale lo
  sceglie Redazia se resta vuoto. Un sito senza destinazioni vede il modulo
  per primo.
- **Fonti e Trend** con un solo testo che spiega (il riquadro in cima);
  l'elenco delle fonti sale, "Aggiungi fonti a mano" va sotto.
- **Profilo in preparazione dice a che punto è**: in coda, in corso con il
  passo, non riuscito con il motivo, mai avviato con il pulsante per
  avviarlo.
- **Il mio piano**: "Cambia piano" per chi ha già un abbonamento attivo;
  "Notizie oggi" solo agli amministratori.
- **Impostazioni**: tolto il riquadro Piano doppio (il piano è un pulsante
  in alto), la tabella dei token compare solo se ce ne sono.

## 0.28.38

Primo lotto dell'audit di amministrazione e redazione.

### Changed

- **Chiavi API spiegate** (Francesco: "cose buttate lì"): in cima alla pagina
  c'è cosa fa una chiave e chi la usa (il sito di vendita, i rivenditori).
  Ogni chiave dice cosa può fare (tutte le redazioni, oppure solo i clienti di
  un rivenditore), quando è stata creata e quando è stata usata l'ultima
  volta. Le revocate sono sbiadite, con la data della revoca. Prima di
  revocare, la conferma dice cosa si ferma (le iscrizioni dal sito finché la
  nuova chiave non è su Vercel, o il rivenditore). Una chiave nuova si crea
  già per un rivenditore, scelto dal menu "Per chi". Migrazione `0041`
  (`operator_tokens.last_used_at`, aggiornato al più ogni 5 minuti).
- **Nuova redazione con il menu dei piani**: il piano si sceglie dal
  catalogo (con prezzo e giorni di prova), la redazione parte con i suoi
  limiti e il suo abbonamento. I limiti a mano stanno in "Limiti su misura",
  chiuso: un numero scritto lì vince su quello del piano, e "Su misura" crea
  la redazione solo con quei limiti, come prima. Nella scheda della
  redazione "Nome, piano e limiti" diventa "Nome e limiti": il piano si
  cambia in Abbonamento, il campo di testo libero non c'è più.
- **Backup riordinato** in tre gruppi: backup automatico, dove si salvano,
  copia su un altro server.
- **Redazione automatica: articoli al giorno contro il piano**: sotto le
  regole c'è quanti articoli fanno in 30 giorni, sommando gli altri siti,
  contro le bozze al mese del piano e quelle rimaste. Se non bastano
  l'avviso è arancione e dice dopo quanti giorni finiscono e cosa succede
  (crediti, o stop fino al mese dopo).

### Fixed

- **Il costo del modello nel Desk lo vedeva anche il cliente**: la colonna
  "Costo 30 giorni" ora è solo per gli amministratori.
- **Codici al posto dei nomi**: le lingue si leggono per nome (Italiano, non
  `it`) in Sito e profilo, Redazione automatica, Primi passi e Impostazioni;
  i piani per nome nell'elenco delle redazioni, nella scheda e in Clienti,
  che dice anche "1 cliente" invece di un numero solo.
- **Controlli delle frasi in inglese**: "no valid source", "numbers not
  found..." e "copies a passage..." nella bozza ora sono nella lingua del
  pannello, e nei motivi della redazione automatica in quella della
  redazione.
- **Titoli doppi** in Clienti, Rivenditori, Piani, Backup, Aggiornamenti e
  Amministratori: restano per i lettori di schermo, non si vedono più.

## 0.28.37

### Fixed

- **Una versione appena uscita non si vedeva subito** (Francesco): il
  server di app.redazia.com, che risponde alle installazioni e le conta,
  teneva l'ultima release di GitHub per dieci minuti, e "Controlla adesso"
  riceveva quella vecchia. Ora la tiene due minuti, e "Controlla adesso" la
  rilegge da GitHub (al più ogni 15 secondi).
- **Il controllo automatico degli aggiornamenti era ogni 6 ore**: ora è ogni
  ora, e aprire la pagina Aggiornamenti controlla da sé se l'ultimo
  controllo ha più di dieci minuti (con un'attesa massima di 5 secondi).
- **I motivi degli articoli scartati erano in inglese** (Francesco, con "Rule
  9: ..."): il caporedattore AI li scrive nella lingua della redazione (se
  non è impostata, in quella del sito), a parole semplici e senza numeri di
  regola; anche i motivi scritti dal programma (fonti troppo poche,
  argomento da evitare, frasi che non superano i controlli) sono tradotti. I
  motivi già salvati restano come sono.

## 0.28.36

### Fixed

- **Articoli che parlano delle proprie fonti** (Francesco, articolo su
  Antonelli: "Cosa è confermato sul titolo", "le fonti consultate", "le fonti
  disponibili non indicano... quindi qui non si possono dare"): chi scrive
  racconta il fatto e attribuisce come un giornalista ("secondo AGI"); ciò
  che le fonti non danno si lascia fuori, a meno che la mancanza sia essa
  stessa la notizia. Tolta dalla scrittura la richiesta di "dire cosa non si
  sa ancora", che produceva questi paragrafi. Il caporedattore rifiuta un
  articolo che parla delle sue fonti o delle sue verifiche invece del fatto.

## 0.28.35

### Fixed

- **Gli articoli scartati non si trovavano** (Francesco): "Vedi tutti" del
  Desk portava a Pubblicazioni, che elenca solo ciò che è stato inviato, e
  gli scartati non hanno una pubblicazione. Ora porta alle Bozze, dove ogni
  articolo scartato o fermato dalla redazione automatica mostra il motivo
  sotto il titolo; nel Desk il motivo compare scritto anche sotto ogni
  scartato (prima era solo un suggerimento al passaggio del mouse).

## 0.28.34

### Added

- **Fonte primaria quando manca il dato centrale** (revisione esterna degli
  articoli di aitoday.it: il comunicato Gimbe, l'annuncio OpenAI): prima di
  scrivere, un controllo a basso costo dice se il fatto centrale (una cifra,
  una decisione, un annuncio) viene da un documento che le fonti date
  riportano solo di seconda mano. Se manca, la redazione segue i link della
  notizia e la pagina iniziale dell'ente che l'ha pubblicata, legge al più
  5 pagine e tiene un documento solo se un secondo controllo conferma ente,
  argomento, data e che il testo dice davvero il dato. Il documento entra
  come ORIGINALE e vince sui giornali. Non usa un motore di ricerca: il Core
  legge pagine, e una pagina suggerita dal modello vale solo dopo la verifica.
  Tetto di 40 chiamate al giorno per redazione, pagine già lette riutilizzate,
  esito ricordato sulla proposta (una riscrittura non cerca di nuovo). Passo
  "Ricerca delle fonti primarie" nei Consumi e instradabile.
- **Refusi**: il caporedattore rilegge titolo, sottotitolo, titolo e
  descrizione per i motori e corregge un refuso chiaro ("i Senato"), solo se
  è un piccolo cambio (oltre il 20% è una riscrittura e non si applica).

### Changed

- **Proposte vicine**: lo stesso argomento non è la stessa notizia. Una
  proposta con almeno una notizia che nessun articolo recente ha usato e un
  titolo non quasi uguale è uno sviluppo nuovo e va avanti, anche se arriva
  entro i due giorni. Restano in attesa solo la stessa storia raccontata di
  nuovo (65% delle parole e almeno 4) o lo stesso argomento senza nulla di
  nuovo (40% e notizie già usate).
- **"Leggi anche"**: la parola rara è solo il primo filtro; i titoli
  collegati devono condividere almeno il 30% del peso delle parole
  dell'articolo (le più rare del sito pesano di più). L'analisi del sito
  aggiorna i titoli già noti con quelli dal vivo.
- **GitHub che sbaglia dal suo lato** (502, 500, 503, 504, limite): ogni
  richiesta di scrittura della pubblicazione si ripete dopo una pausa (sono
  tutte ripetibili senza danno); il merge no, ma prima di ripeterlo guarda se
  la pull request è già unita. Se la pubblicazione fallisce comunque per un
  errore del genere, riprova da sola due volte (dopo 30 e 120 secondi).

## 0.28.33

### Fixed

- **Le barre dei limiti sul Desk** restavano vuote: la classe `meter` era
  già usata da un'altra tabella e ne cambiava la forma. Ora si riempiono, e
  i riquadri stanno su colonne uguali.
- **Proposte che si sovrappongono** (revisione esterna degli articoli di
  aitoday.it): la redazione automatica non sceglie una proposta che racconta
  ciò che un articolo degli ultimi due giorni, o uno già scelto nello stesso
  giro, racconta già (almeno 3 parole in comune e il 40% del titolo più
  corto). Resta in attesa e può diventare un aggiornamento.
- **Caporedattore**: prima di chiamare contraddizione due affermazioni
  controlla che parlino dello stesso momento, della stessa misura e dello
  stesso perimetro (conteggio parziale contro risultato, "prima donna nera
  governatrice di uno Stato" contro "prima governatrice nera della Georgia").
- **Pagine in aggiornamento** (dirette): la bozza riceve l'ora di lettura di
  ogni fonte e sa quando la stessa pagina è arrivata con titoli diversi,
  così racconta ciò che diceva a quell'ora.
- **"Leggi anche"**: il titolo di un articolo modificato dopo l'uscita si
  aggiorna; un link richiede almeno una parola rara del sito in comune
  (due parole comuni come "stati uniti" collegavano pezzi senza legame).

## 0.28.32

### Added

- **I limiti sul Desk** (Francesco): un riquadro compatto con i tetti del
  piano e a che punto si è: articoli di oggi della redazione automatica,
  articoli del mese, fonti attive, siti, notizie lette oggi, crediti. Verde
  se il tetto è lontano, giallo da 80%, rosso quando è raggiunto (con
  "riprende domani" per il tetto giornaliero). I limiti assenti non compaiono.

## 0.28.31

### Fixed

- **Desk di 14 secondi, Trend e piano di 6** (log del server di Francesco: sul
  Desk solo 1,3 s nel database): il Desk ricalcolava a ogni apertura le
  notizie "rimaste fuori dal tetto", leggendo e confrontando fino a 3000
  notizie per ognuna delle 80 proposte. Ora lo conta la redazione automatica
  una volta all'ora e il Desk legge il numero. In Trend e piano una sola
  query impiegava 4,9 s: mancava l'indice dei punteggi di opportunità per
  tema (migrazione 0040, sqlite 0029) e le fonti seguite si cercavano con un
  OR nel join; ora sono lette una volta, con lo stesso schema del Desk.

## 0.28.30

### Changed

- **La ricerca fonti dice perché** (Francesco: le fonti restano 44): il
  risultato in Attività riporta ora le note della ricerca (modello che non
  risponde, limite di fonti del piano raggiunto, fonti illeggibili), invece
  del solo "nessuna fonte nuova".

## 0.28.29

### Fixed

- **Desk e pagine lente, con tante notizie in archivio** (Francesco: quasi 10
  secondi): il conteggio delle notizie di un sito chiedeva al database, per
  ogni notizia dell'archivio, di scorrere tutte le fonti. Ora legge prima
  l'elenco delle fonti seguite, una volta sola. Il Desk con 300.000 notizie
  in archivio passa da circa 3 secondi a 0,3. "Ultime notizie" guarda solo
  gli ultimi tre giorni e il passo "Raccogli le notizie" dei Primi passi
  cerca una notizia sola invece di contarle tutte.

## 0.28.28

### Changed

- **Il Desk si compatta da solo** (Francesco): "Da decidere" compare solo se
  c'è qualcosa da decidere, e allora Attività e Raccolta si affiancano invece
  di lasciare un buco; i riquadri "Proposte nel piano" e "Bozze pronte" solo
  se non sono a zero; "Siti più attivi" e "Siti più costosi" solo con più di un
  sito; il riquadro "Come funziona" solo all'inizio. L'elenco degli articoli
  del giorno è più corto, con il link a Pubblicazioni.

### Fixed

- **Entrare in una redazione impiegava 4-5 secondi**: le notizie del giorno si
  contavano leggendo tutto l'archivio, perché mancava un indice sul momento
  di lettura (migrazione 0039, sqlite 0028). Ora leggono solo il giorno.
- Una pagina che impiega più di un secondo e mezzo scrive nel log perché:
  tempo totale, tempo nel database e le tre istruzioni più lente
  (`docker compose logs editor | grep "slow page"`).

## 0.28.27

### Changed

- **Sito e profilo in quattro schede** (Francesco): *Profilo*, *Redazione
  automatica*, *Pubblicazione*, *Il sito*. Ogni scheda mostra solo il suo
  contenuto, niente più triangoli da aprire. Tolti i riquadri "Il sito in
  numeri" (sono nel Desk), l'elenco degli articoli del giorno (rimanda a
  Pubblicazioni) e le percentuali dei temi quando non c'è nulla da
  confrontare: restano i temi con le parole chiave e "Togli il tema".
  Le impostazioni sparse (proposte al giorno, copertine, fonti negli articoli)
  stanno in un unico riquadro "Come scrive il sito"; le regole della redazione
  automatica sono sempre visibili nella loro scheda.
- Lingue, feed e Search Console stanno in *Pubblicazione*; Search Console non
  compare se il server non ha la chiave (prima chiedeva "all'amministratore").
- "Elimina il sito" è l'ultima riga di *Il sito* e chiede due passaggi: link,
  poi il dominio da scrivere. Versioni del profilo spostate lì.
- Corretto: il riassunto di un profilo nato dal contesto mostrava il dizionario
  grezzo invece del testo.

## 0.28.26

### Added

- **Oltre gli articoli del giorno** (Francesco): con la redazione automatica,
  quando i `per_day` articoli sono fatti e arriva una notizia importante
  (opportunità almeno `extra_min_opportunity`, fonti diverse come sempre) può
  uscirne una in più: fino a `borrow` articoli **anticipati dal giorno dopo**
  (gratis: domani ne scrive altrettanti in meno) e fino a `extra_credits`
  **con i crediti** (0 = mai; il tetto è per giorno, uno a ogni giro). Le
  impostazioni sono in Sito e profilo › Regole e impostazioni; lo stato del
  giorno sul Desk ne tiene conto.
- Trend e piano di un sito automatico: le proposte dicono che le sceglie la
  redazione; «Accetta» e «Scarta» lasciano il posto a «Scrivila ora».

## 0.28.25

### Changed

- **Desk di un sito automatico** (Francesco: dalle 14:46 non pubblicava più
  e non si capiva perché, né dove vedere gli articoli; "se è automatico
  decidono loro"): un riquadro «La redazione automatica di … oggi» dice
  quanti articoli ha pubblicato, scartato e fermato, **perché si è fermata**
  («ha fatto gli articoli di oggi, 6 su 6, riprende domani alle 07:00»,
  fuori orario, piano finito…) e li elenca con il link all'articolo online
  e «modifica o ritira». Le proposte di un sito automatico non sono più
  «da decidere» (né contate né elencate): quelle non usate scadono dopo un
  giorno e mezzo (`autopilot.lapse`).
- **Sito e profilo** più corto: numeri, versioni, lingue, Search Console,
  come ricevi gli articoli, dati del sito e «aggiungi un dominio» sono
  sezioni chiuse; la redazione automatica mostra lo stato e tiene le regole
  in una sezione che si apre; il testo di spiegazione compare solo quando è
  spenta.
- **Più fonti per i siti generalisti**: la ricerca delle fonti chiede al
  modello i siti da seguire una volta ogni 3 temi (fino a 8 domande) invece
  di una sola da 25, e non ripropone quelli già seguiti, così ogni giro
  quotidiano ne trova di nuovi.

## 0.28.24

### Added

- **Collega il sito**, il percorso guidato (Francesco: caricato un sito si
  guida la persona a validarlo, fino ad avere un sito aggiornabile). Dopo il
  dominio, e prima dell'analisi, la pagina `/connect` chiede con cosa è fatto
  il sito, spiega dove creare la credenziale e **prova il collegamento prima
  di salvare qualsiasi cosa**:
  - **WordPress**: passi per la password applicativa; prova l'accesso, il
    ruolo (pubblica o solo bozze), il caricamento della copertina e scarica
    le **categorie**. Gli articoli escono nella categoria della loro
    rubrica, e l'analisi parte già dalle categorie del sito: diventano le
    rubriche della redazione automatica;
  - **GitHub**: passi per il token fine-grained con i permessi esatti, e
    per collegare il repository a Vercel, Cloudflare Pages, Netlify o
    GitHub Pages (con il modello pubblico per chi non ha ancora un sito).
    Prova il token sul repository, legge com'è fatto (Astro, Next.js, Hugo…,
    quale servizio lo pubblica), trova la cartella degli articoli e ne copia
    il formato; l'anteprima da aspettare si attiva solo dove esiste;
  - **altro sistema**: indirizzo e chiave del webhook, con il rimando alle
    linee guida pubbliche;
  - **più tardi**: legge il sito e scrive le bozze senza destinazione.
  Ogni errore dice cosa fare (password sbagliata, ruolo senza permessi,
  token scaduto, repository non abilitato…); la credenziale è salvata cifrata
  e collegare di nuovo lo stesso sito aggiorna la destinazione senza
  duplicarla. In Primi passi il collegamento è il secondo passo (quello di
  pubblicazione non è più l'ultimo); un sito già letto senza destinazione
  non blocca gli altri passi e il collegamento resta da fare.

### Changed

- Aggiungere un sito non avvia più l'analisi da solo: porta a Collega il
  sito, che la avvia a fine percorso (o con «Analizza senza collegare»).
  L'iscrizione dal sito e dall'API non cambia.

## 0.28.23

### Added

- The director keeps a published article in hand (Francesco: the automatic
  newsroom is an extra, a person's control is needed in every newsroom):
  - **free editor** of the whole article on the draft page: one sentence
    per line, `## ` for a heading, `[1] [2]` for the sources from the list
    beside it. Sentences can be added, removed and moved; a sentence
    without a source is the director's and goes out as written; cited
    ones are checked as usual. An approved draft is approved again;
  - **Aggiorna sul sito**: the published article replaced with the
    approved text, at the same address (WordPress: the same post;
    GitHub: the same file, llms.txt line updated; webhook: event
    `update`). A withdrawn article goes back online the same way;
  - **Ritira dal sito**, with a confirmation: WordPress moves the post to
    the bin, GitHub deletes the file and its cover and removes it from
    llms.txt and the sitemap, webhook gets event `withdraw`, Telegram
    deletes the message. The translations published from it go too, and
    it is no longer linked by "Leggi anche";
  - **Rifai l'articolo**, with a note on what to change: a new version of
    the same proposal, which never goes out by itself; once approved,
    "Pubblica al posto della versione online" puts it at the same
    address.
  Each change is a job of its own (Attività) and a failure leaves the
  article as it was, with the reason on the draft page. Migration 0038 /
  SQLite 0027: `publications.updated_at/by`, `withdrawn_at/by`.

### Changed

- The newsroom sees its plan's units, never the model's euros: "Costo
  modello, 30 giorni" (Sito e profilo, Desk) and "Siti più costosi" are
  shown to the operators only. The newsroom sees "Crediti del mese"
  instead: the credits left of the month's, the extra ones, and a link to
  the plan.

## 0.28.22

### Fixed

- "Leggi anche" was empty on sites written by the newsroom: the links come
  from the site's own articles, and those were only the ones found by the
  site analysis. Every article the newsroom publishes on the site
  (WordPress, GitHub, webhook; not as a draft, not the translations) is
  now one of them as soon as it is out, and the ones published before
  are added the next time an article is written. The proposals also know
  the site already told that story.
- A link to a related article needs two shared words of the title, or one
  word the site rarely uses (a company, a person, a product): on a site
  about AI, "Apple" links the Apple articles, "intelligenza" alone does
  not. The closest come first.

## 0.28.21

### Fixed

- A publication to GitHub no longer fails at the first server error of
  GitHub's: a read answered with 500, 502, 503, 504 or 429 is tried again
  after 2, 5 and 15 seconds (on 2026-11-03 two articles of aitoday.it
  failed on "reading llms.txt: 500"). Writes are never repeated, since
  they may have gone through.

## 0.28.20

### Changed

- Articles of the automatic newsroom, after an external audit of 14
  articles on aitoday.it (2026-11-03):
  - **one event per article.** The article is written from the cited news
    closest to the proposal's title and from other outlets' reports of the
    same event (at least half of its title's words, three at least). News
    of the same sector that is about something else is no longer given to
    the model nor counted as a second source: it made articles join
    separate stories (Trump with Amazon and Oracle, the US GDP with
    Aramco). Up to 4 earlier news on the subject come apart, marked as
    context: the model uses one only as a precedent, a comparison or the
    rule that applies, says which, and never as a second story; they do
    not count as sources.
  - **the original document.** When the main news links an official
    document (statistics offices, central banks, governments, the EU,
    authorities, laws, papers), it is read and given to the model as
    citable, and the central numbers, records and decisions follow it,
    not the outlet: the GDP article repeated the outlet's mistake.
  - **as long as the sources allow.** No minimum of 900 words and no
    second call asking to "expand to the target": with one outlet, or only
    a title or an abstract, the model writes a short news piece and says
    what is not known yet; even short, it must give what its title
    promises (the figure, the decision, what the study found). Questions and answers only when they add
    something, no longer two to four in every article. One model call per
    draft instead of two in most cases.
  - **exact.** A number with what it is compared to, superlatives,
    "firsts" and causes only when a source says them and attributed, the
    date of the event and not of the republication, title and opening no
    more certain than the body.
  - **the AI editor-in-chief reads the sources.** It gets what was read
    of each cited source and refuses an article that joins separate
    events, states a number, superlative or cause the sources do not
    state, has a title or opening more certain than the body, misses the
    essential information its title promises, or gives a central figure
    different from the original document.
  - **arXiv in full.** An arXiv abstract page is read as the paper's HTML
    version.
- The checklist of a draft no longer asks for two questions and answers
  and for a length close to the site's articles.

### Added

- `editor redraft <domain> --tenant <t> --since <date>`: writes again,
  with the current writing and without publishing, the articles a site
  published since a date (14 at most by default, no new cover), so the
  new version can be compared with the one that went out in Bozze.

## 0.28.19

### Added

- Impostazioni › I tuoi dati › "Esporta la redazione (ZIP)": the whole
  newsroom in a file anyone can open. The approved and published articles
  (and, apart, the other drafts) as Markdown with front matter and as an
  HTML page, each with its cover; the approved editorial profile of each
  site with the automatic newsroom's settings; sources, destinations and
  publications in CSV; every row in `data.json` for a technician; a
  README. Passwords, tokens and access stay on the server, as before. The
  JSON alone is still there ("Solo i dati (JSON)", and `/api/export`).

## 0.28.18

### Changed

- The news base read for every newsroom goes from 64 to 146 sources, each
  checked to answer before being listed: more of the United States, the
  United Kingdom, Asia, the Middle East, Africa, Latin America and Russia,
  more economy and technology (AI labs included), and two new sections in
  Indice notizie, "Scienza e ambiente" and "Cultura e spettacolo". More
  outlets tell the same story, so more stories pass the "different
  sources" rule.
- The worker runs 4 jobs at a time by default (was 2): with the automatic
  newsroom, the news collection and the translations, 2 left articles
  waiting. `EDITOR_WORKER_CONCURRENCY` in `.env` still sets it.

### Fixed

- Attività no longer says the worker seems stopped while it is working:
  with a long queue the running job fell out of the list the warning
  looked at.

## 0.28.17

### Added

- Console › Backup: backups of the server set from the console. Daily or
  weekly, at the time chosen, in the folder chosen (under /home,
  /var/backups, /backup, /backups, /mnt, /media, /srv or /opt, never in a
  folder a web server publishes), with the days to keep, the owner and
  the file name; "Backup adesso"; the archives on the server, each one
  deletable; the log. Optional copy to another server with rsync over SSH:
  the server makes its own key and the console shows the public one to
  add to the remote `authorized_keys`. `update.sh` runs them, as it runs
  updates, and the backup before an update goes to the same folder.

### Fixed

- The automatic newsroom stopped for hours with "nessuna proposta adatta"
  while 121 proposals waited: the "different sources" rule counted only
  the news of a proposal's topic. It now counts every news the article is
  written from, as the writer reads it: the cited news, those of its
  topic and the other recent news on the same story from any outlet.

### Security

- `update.sh` no longer writes through files of the shared folder (the
  dashboard's user writes there): it works in a folder of its own and
  moves the results in, and renews itself from there.

## 0.28.16

### Changed

- Updates from the console start at once: `update.sh` is started by a
  systemd path unit as soon as the request is written, instead of waiting
  for the cron minute (kept as a fallback, and for servers without
  systemd).
- Console › Aggiornamenti shows a progress bar with the percentage:
  backup, download of the images (from the progress of each layer),
  restart and check, read every 2 seconds; the page reloads by itself when
  the new version answers.
- `update.sh` renews itself after each update with the script of the
  installed version, and installs the systemd units on its own: from now
  on nothing to run by hand. A server whose `update.sh` is older than
  0.28.16 cannot renew it (the old script does not know how): there, once,
  as root, `curl -fsSL -o update.sh
  https://raw.githubusercontent.com/eullm/eullm-agent/v0.28.16/deploy/server/update.sh
  && sh update.sh install`.

## 0.28.15

### Fixed

- The automatic newsroom no longer stops in the morning when the server
  came up after the first hour of its window: each hour it checks how many
  articles the day should have by then (the morning's share, then the rest
  spread evenly) and catches up on what is missing.
- A strong story told by one source is no longer left aside: the
  newsroom takes stories down to half the minimum opportunity, best first,
  when there are not enough above it. The "different sources" rule still
  applies.
- A site with its translations under /en is no longer read as an English
  site: the language its home page declares stays when a fair share of the
  articles read are in it. "Lingua del sito" shows the language of the
  approved profile, the one the articles are written in.

## 0.28.14

The v0.28.13 tag went on the 0.28.12 commit, so its images held 0.28.12:
this version brings what 0.28.13 lists.

### Changed

- Console › Aggiornamenti: while an update is asked or running, every page
  of the console says so instead of offering it again. An update that
  finished while the server still answers with the older version (images
  holding another version) is said plainly, and the same version is not
  offered again.

## 0.28.13

### Changed

- The automatic newsroom publishes the morning first: in the first hour of
  its window it takes on its share of the day's articles (half by
  default, "Al mattino (%)"), the rest is spread over the day.
- A story counts all the sources that tell it: the "different sources"
  rule reads the cited news and the other news of its topic, the same the
  article is written from. Stories picked from one feed and told by
  others no longer wait for nothing.
- A translation published to a site's repository names its original
  (`translation_of`, the original's slug, in the front matter), so the
  site's language switch leads from one to the other.

## 0.28.12

### Added

- Sources added by hand, in Fonti: the addresses of sites or feeds, up to
  20 at a time. The agent finds and reads each feed; those a person chose
  enter active, within the plan's limit, even outside the profile's topics.
- The automatic newsroom looks for sources by itself once a day and takes
  on the candidates, within the plan: nobody has to pick them.
- Source suggestions for sites on world news include the main outlets of
  other countries (United States, United Kingdom, Germany, France, China,
  Japan, India), up to 25 domains.
- The news base read by every newsroom grows from 46 to 64 feeds, with the
  United States, China, Japan, India, Singapore and Australia, plus the
  Frankfurter Allgemeine and Le Figaro.

## 0.28.11

### Changed

- The automatic newsroom stays within the plan's articles of the month set
  in the console: it never takes on more than are left and, when they are
  used up, it waits for the next month and says so on the site page.

## 0.28.10

### Changed

- The automatic newsroom's articles a day are articles kept: one it
  discards is replaced by another. It tries at most twice the articles of
  the day, so the cost stays known; past that it stops until the next day
  and says so on the site page ("Ultimo giro").
- A translation asked by hand of an article the automatic newsroom
  published is approved and published by itself, like the original.

## 0.28.9

### Changed

- Articles read as written by a person: dashes used as pauses (em dash,
  en dash, double hyphen) and ellipses are taken out of every sentence,
  title, heading and question, in drafts and translations; the writing
  prompt forbids them too.
- A site with a destination set for another language publishes in it: the
  automatic newsroom translates every article into the languages of the
  site's destinations as well as those chosen, within the plan.

## 0.28.8

### Changed

- The automatic newsroom never stops to wait for a person (new setting
  "Se un articolo non passa", default "fix"): an article that fails the
  source checks, cites too few sources or is refused by the AI
  editor-in-chief is written again once, with the reasons, by the model of
  the new step "Correzione degli articoli scartati" (the stronger model
  chosen in the console, else the default); if it still does not pass it
  is discarded. An article on a topic to avoid is discarded at once. A
  translation that fails is discarded, the original stays published. The
  old behaviour stays available as "Lo ferma e aspetta me".
- The cover is drawn only after an article passed every check and the
  editor-in-chief: a discarded article costs no image.
- Activity and Redazione automatica say what was discarded and why, and
  when an article was written again.

### Fixed

- A destination set for one language takes only articles in that
  language: the draft page lists only the destinations of the draft's
  language, and a request towards another one is refused, so an Italian
  article can no longer go to the English site.
- Publication errors in plain words: a GitHub token that cannot see the
  repository says what to set in the token.

## 0.28.7

### Changed

- With the automatic newsroom on, the pages stop asking for decisions it
  takes by itself: the site's numbers say "Proposte in coda" picked by the
  automatic newsroom, Desk marks its proposals, Primi passi waits on it
  instead of "Tocca a te", and "Proposte al giorno" says it is the morning
  number for deciding by hand, apart from the articles a day.
- Redazione automatica shows its last round: not done yet, outside hours,
  stopped by the plan, the day's articles done, proposals picked, or no
  proposal good enough and why (minimum opportunity and sources).

## 0.28.6

### Changed

- The side menu stays in view while a long page scrolls, in the newsroom
  and in the console: Il mio piano, Guida, Impostazioni, the person, Esci
  and the version are always at the bottom of the screen; on a short
  screen the upper part of the menu scrolls on its own.

## 0.28.5

### Changed

- A site with too few articles to read (fewer than 5, as a new site) gets
  "Profilo da completare" in Primi passi and Sito e profilo instead of
  "Approva il profilo": the profile read from the home page and the
  sections is a guess, so the page asks for the context in a few lines
  and writes the profile from it ("Crea il profilo dal contesto"), then
  it is approved as usual. The other automatic newsroom settings stay.

## 0.28.4

### Changed

- Nuova redazione: with an existing customer chosen, the director's email
  can stay empty and the customer's email is used.

## 0.28.3

### Added

- Nuova redazione in the console asks for its customer: "Nuovo cliente"
  (chosen by default) opens the new newsroom's card with the customer's
  details to fill in; an existing customer is linked at once.

## 0.28.2

### Added

- The console's newsroom card has "Persone e cliente": who works in the
  newsroom (email, role, invitation still to accept) and its customer. A
  newsroom made before billing, or without one, gets its customer here:
  link one the console already has, or fill in the billing details to
  create it. Clienti lists the newsrooms without a customer.

## 0.28.1

### Fixed

- A refund cancelled in Mollie's dashboard (for example when the balance
  has no funds) showed in the console as if it had left. Refunds not
  finished yet are read again from Mollie when a newsroom's card opens and
  every hour by the worker: queued, in progress, refunded or cancelled, in
  the newsroom's language. Credit payments are named in the list.

## 0.28.0

### Added

- Credits: what goes beyond the plan is paid with credits, never with a
  surprise on the bill. Every plan gives credits each month (`credits_month`,
  set in Piani and on each newsroom; trial 30, Solo 100, Starter 250, Pro 800,
  Business 2500, Agency 8000). They pay for an article or a translation past
  the plan's articles of the month (25 and 10 credits) and for a cover drawn
  again after the first one and two free redraws of each article (3
  credits). The month's credits are used first and start again on the 1st;
  then the extra credits, which do not expire.
- Packs of extra credits (500 for 5 €, 2.000 for 18 €, 6.000 for 48 €),
  bought by the owner in Il mio piano with a single Mollie payment, or
  recorded by the operator in the console (bank transfer with its amount,
  a gift, or a negative correction). Prices, packs and each newsroom's
  credits are edited and deleted from the console (Piani › Crediti and the
  newsroom's card).
- Il mio piano shows the month's credits, the extra ones, the price list,
  the packs and the latest movements; the draft page says how many free
  covers are left or what the next one costs.
- The owners get an email at 80% and at 100% of the month's credits.
- `/api/public/plans` gives each plan's credits, the price list and the packs.

### Changed

- The automatic newsroom stays within the plan's articles: credits are
  spent only on what a person asks for.
- A newsroom without monthly credits (the local installation, a newsroom
  without a plan) works as before: nothing is charged or stopped.

## 0.27.1

### Fixed

- Covers with a FLUX model on OpenRouter (`EDITOR_IMAGES_MODEL=
  black-forest-labs/...`) failed with "does not support: quality": the
  quality is now sent only to OpenAI's image models, and an option a
  model refuses is dropped and the image asked again.
- With one site chosen in the menu, the Attività box showed the work of
  every site again a few seconds later: the box refreshed while work runs
  ignored the choice. It now follows the menu on every page and on the
  Attività page: one site, its work and the collection; "Tutti i siti",
  everything.

## 0.27.0

### Added

- Weekly report by email (`editor/weekly.py`): every Monday at the hour
  of the briefing, the newsroom's week in its own language, per site:
  articles published with their links, news read, proposals and drafts,
  clicks and searches from Google, what is waiting for a decision and
  what comes next week, and the articles of the month against the plan.
  It goes to the email recipients or, with none, to the directors; a
  worker that was down sends it until Wednesday and a refused email is
  tried again every hour. Impostazioni has the list of reports, a preview
  of the last seven days, "Mandamelo adesso" and the switch in
  Preferenze (`tenants.weekly_report`, table `weekly_reports`).
- Google Search Console per site (`editor/searchconsole.py`): one service
  account for the installation (`EDITOR_GSC_KEY`, base64 of the JSON key,
  or `EDITOR_GSC_KEY_FILE`); the customer adds its email to the property
  in Search Console and writes the property in Sito e profilo, where the
  last 28 days show clicks, impressions, position, the pages and the
  searches that bring the most visits. Read every night at the
  maintenance (`search_stats`, sixteen months kept). A topic matching
  what people search to reach the site gets up to 10 points more in the
  Opportunity Score (component `search`, a bonus: sites without Search
  Console score as before; formula `opportunity-v2`).
- Primi passi (`/start`): the way from a domain to the first published
  article as a guided path, the current step open with what it needs on
  the same page (add the domain, approve the profile, activate the
  candidate sources, collect the news, accept a proposal, read the
  draft, connect the site). In the menu until the site in view can
  publish, and as a box on the Desk.
- Migrations 0036 (PostgreSQL) and 0025 (SQLite).

## 0.26.6

### Changed

- The newsroom pages use the same panels as the console: editing a
  publication, notification preferences and recipients, suspended
  sources, billing details and cancelling the subscription open as
  coloured panels with an icon instead of the small grey triangles;
  the small "more" links in Sources, Trends and the login page share
  one accented style.

## 0.26.5

### Changed

- Topic names cost less for the same names: every hour the model named
  again the ten largest topics that got news, even when one headline
  had joined a topic it had named an hour before. A topic named by the
  model is now named again only when it has really grown (half again,
  and at least 5 more items); the size at naming is kept in
  `topics.labelled_items` (migrations 0035, sqlite 0024).

## 0.26.4

### Fixed

- /admin/usage (Consumi) kept its old menu: it now has the menu of the
  rest of the console, with its three sections (model per step,
  reference prices, cost per article) under Consumi.

## 0.26.3

### Fixed

- An administrator who entered a newsroom from the console ("Entra") and
  then deleted it was sent back to the login: the session belonged to
  that newsroom and went with it. It now goes back to the console.

### Changed

- The console is split in pages, one for each entry of the menu
  (/admin/customers, /admin/plans, /admin/resellers, /admin/operators,
  /admin/keys, /admin/updates, /admin/new) instead of one long page.
  Redazioni is a compact table, one row per newsroom with plan, status,
  sites, drafts and model cost, and a search box; "Apri" shows the whole
  newsroom at /admin/newsrooms/<id>. The plans are closed bars with
  their prices, and only the one being changed opens. Every form comes
  back to its page.
- While an update runs, /admin › Aggiornamenti says the panel stops for
  a few seconds and comes back by itself, and reloads on its own once
  the new version answers (`/api/health` now also gives the version), so
  the errors of the reverse proxy during the restart are not seen.
- deploy/server/README.md, "During a restart": why a restart loses
  nothing (migrations in one transaction, jobs back in the queue) and an
  Apache page that reloads itself during those seconds.

## 0.26.2

### Fixed

- After a refund that stopped the renewals, changing the plan of that
  newsroom failed with "the automatic renewals on Mollie were not
  cancelled": the console asked Mollie to cancel them a second time and
  Mollie refused. Renewals already cancelled (or completed) now count as
  cancelled.
- The emails to the administrators (new newsroom, payment, refund) end
  with an "Open in the console" button that goes to that newsroom, not a
  bare link to the customers list.

## 0.26.1

### Fixed

- Deleting a plan counted a newsroom twice (once for its subscription,
  once for its own row), so a plan used by one newsroom said "2
  newsrooms". The console now names the newsrooms still on the plan and
  says where to change it (Newsrooms › Subscription).
- After an action in the console (a refund, a plan change, an edit of a
  newsroom, customer or reseller) the page came back at the top with
  everything closed. It now comes back to the same newsroom, with the
  panel used still open and the result shown inside the card.
- A form is sent once: the button is disabled after the first click, so
  a second click cannot repeat a refund or an invite.

### Changed

- The panels that open in the console (Subscription, Payments and
  refunds, Name plan and limits, Invite, Delete) are full bars with a
  coloured badge and a chevron instead of a small grey triangle; the
  "edit or delete" links in the newsroom pages are buttons.

## 0.26.0

### Added

- Refunds from the console. In /admin, each newsroom has "Payments and
  refunds": its latest payments with the Mollie ID, and for a paid
  online payment a form to give back all of it or part. After the
  refund, the subscription can stay as it is, stop renewing (the
  newsroom works until the end of the period) or stop now with the
  newsroom. The amount still refundable is read from Mollie, so a
  payment cannot be given back twice. The refund is recorded among the
  payments and the administrators get an email. Migration 0034 (SQLite
  0023).

## 0.25.3

### Changed

- Every email from Redazia goes out in HTML as well as plain text: the
  logo, the subject as a title, the link as a button with the address
  under it, lists as lists, the company in the footer. The HTML is built
  from the text, so invitations, password resets, trial reminders and
  payment emails all change at once.
- The invitation tells what Redazia does and the three first steps, and
  no longer reads "in la redazione".

### Fixed

- The payment confirmation goes to the customer in the newsroom's
  language, and the payment notice to the administrators in the
  language of `EDITOR_LANGUAGE` (new line in `compose.yaml`, default
  `en`): a Mollie notice comes with no browser language, so both went
  out in English.
- In the form of an invitation the email comes right before the
  passwords, marked as the username: browsers save the email as the
  login, not the name.

## 0.25.2

### Added

- On a phone the menu folds into a hamburger (below 820 px), opened
  without scripts.

### Fixed

- The logo in the menu of the newsroom and of the console is an image
  again (after the hamburger menu it showed as text).

## 0.25.1

### Fixed

- The Desk with one site chosen: "Da decidere", "Proposte nel piano",
  "Fonti attive", the collection box (active sources, news today, last
  run) and Attività now count that site only. "Da decidere" and
  "Proposte nel piano" also count every proposal waiting, not the first
  12 listed.
- The Redazia logo shows at once: the logo and icon files have new names,
  so browsers do not keep EuLLM's from their cache.

## 0.25.0

### Changed

- The Desk follows the site menu: "Tutti i siti", the first entry and the
  default, shows the whole newsroom as before; choosing a site shows only
  its decisions, work and numbers. The pages of one site keep the last
  site chosen; choosing "Tutti i siti" from any page opens the Desk.
- Plain names for the roles: the console's operators are now
  administrators ("amministratore"), and in a newsroom owner becomes
  "direttore" (editor-in-chief) and editor "redattore", in every
  language. The roles stored and the API do not change.
- The Redazia logo and favicon in the dashboard, the console and the
  sign-in pages, in place of EuLLM's.

## 0.24.0

### Added

- Installations counted: the check for updates (every six hours, also on
  installations without the console update folder) asks
  app.redazia.com/api/public/latest, which answers with GitHub's latest
  release and records a random identifier (derived from
  `EDITOR_SECRET_KEY`, not reversible) and the installed version. Nothing
  else is sent. /admin › Aggiornamenti › "Installazioni di Redazia" shows
  the installations active in 7 days by version, new ones in 30 days and
  all those seen. `EDITOR_TELEMETRY=off` sends nothing and asks GitHub
  directly; if app.redazia.com does not answer, GitHub is asked.
  Migrations 0033 (Postgres) and 0022 (SQLite).

## 0.23.0

### Added

- A model per step: /admin › Consumi › "Modello per passo" chooses, for
  each step (site analysis, profile from context, source search, topic
  names, proposals, drafts, translations, AI managing editor), a model of
  core.yaml's `models` section, for example a fast one for proposals and a
  stronger one for drafts; empty is the Core's default. A name core.yaml
  does not know stops nothing: the default answers and the log says so.
  Migrations 0032 (Postgres) and 0021 (SQLite).

### Changed

- The newsroom no longer shows which model it uses (it was under the menu).

## 0.22.2

### Changed

- The product is called Redazia everywhere a customer sees it: the Mollie
  payment description ("Redazia Solo (anno)"), the subscription, the
  emails (invite, password reset, "redazione pronta"), page titles and the
  version under the menu. A newsroom bought on the site is welcomed with
  "Ti diamo il benvenuto in ... su Redazia".
- Back from Mollie the customer is still signed in (session cookie
  SameSite=Lax; forms keep refusing other sites by their Origin).

### Added

- Emails to the console's people ("[Redazia] ..." in the subject) for every
  new newsroom from the site or a reseller, and for each payment received,
  renewal paid, renewal failed or first payment failed; the customer gets a
  payment confirmation with the date the plan runs to.

## 0.22.1

### Changed

- Buying a paid plan on the public site ("Acquista") no longer starts a
  trial: the newsroom is made on the plan bought, stopped ("Da pagare")
  until its payment is confirmed, and its owner lands on the payment when
  signing in. A buyer who already has an account gets "la redazione è
  pronta: completa il pagamento" instead of the plain "ready" email.
- The plan choice in /account no longer offers the free trial; the payment
  button sits level with the renewal choice; the billing data are laid out
  in three columns in a sensible order.
- Mollie's "No suitable payment methods found" (no recurring method such as
  credit cards enabled in the Mollie profile) shows the customer a plain
  message and tells the operator in the log what to enable.
- Console: the installed version at the foot of the side menu, as in the
  newsroom.

## 0.22.0

### Added

- Every proposal explains itself: "Perché oggi" (how many sources cover the
  news and how recent it is, plus the model's reason), the site's own
  articles it completes, and, when the site already has a very similar
  article, "Meglio aggiornare un tuo articolo" with its link.
- Drafts know the site's articles: when the proposal is close to an article
  already published, the model writes the updated version leading with what
  is new instead of retelling it; related articles are linked in "Leggi
  anche" before the other internal links.
- Discard reasons: "Scarta" asks why (già trattato, fuori tema, troppo
  generico, fonte poco affidabile, or just discard). Each reason weighs on
  the next scores, the titles discarded as already covered, off topic or too
  generic go to the model that writes the next proposals, and an unreliable
  source loses reliability for that site (kept when the source is rated
  again). Sito e profilo sums up why proposals were discarded in the last 30
  days. Migrations 0031 (Postgres) and 0020 (SQLite).

## 0.21.1

### Changed

- Console: "Controlla adesso", "Aggiorna alla X.Y.Z" and the plan forms
  bring you back to their own section (/admin#aggiornamenti, #piani), with
  the outcome shown there, instead of the top of the page.

## 0.21.0

### Added

- After the trial, proposals only: a newsroom whose trial (or paid period,
  after the grace days) ends is no longer stopped at once. For 30 days it
  keeps receiving two article proposals a day per site, with no drafts, no
  translations, no publishing and no automatic newsroom; the owner gets an
  email and every page says so, with the way to choose a plan. After 30
  days without a plan the newsroom stops as before (data kept). Migration
  0030 (`tenants.proposals_only`).
- Plans with or without the automatic newsroom (`plans.autopilot`, field
  "Redazione automatica" in /admin › Piani; `/api/public/plans` says it).
  Without it, the rules stay saved and the site page says the plan does not
  include it.
- Two plans join the catalogue: Solo (19,90 € a month, 1 site, 1 language,
  10 articles, 15 sources, no automatic newsroom) and Agency (999 €, 30
  sites, 5 languages, 1500 articles, 1000 sources). Prices and limits can
  be changed in /admin › Piani as always.
- Bought on the site: who chose a paid plan ("Acquista" on redazia.com)
  goes straight to its payment in /account after setting the password.

### Changed

- The worker stops in seconds: on SIGTERM it gives the running jobs 20
  seconds, then puts them back in the queue (attempt not spent) and exits.
  Before, a restart or an update could wait up to two minutes for a long
  model call. `stop_grace_period` of the worker in compose: 30s.
- Il mio piano: on trial the menu and the page say the days left ("Prova ·
  5 giorni rimasti"); the billing details are a two-column form instead of
  one long row; the menu entry shows the plan under its name.

## 0.20.2

### Added

- Server installation, updates from the console: /admin › Aggiornamenti
  shows the installed version and the latest release (checked on GitHub
  every six hours, or now), and "Aggiorna alla X.Y.Z" updates the server.
  The dashboard gets no Docker socket and no root: it leaves the request in
  a folder shared with the host, where `deploy/server/update.sh` (root's
  cron, every minute) backs up, pulls the images, writes the version in
  `.env`, restarts and checks; the console shows how it went and the log.
  `server-init` now writes `backup.sh` and `update.sh` too.

### Fixed

- SEO: a search title with no space in its first 61 characters is cut
  to the limit too.
- A site whose language was not read yet ("und") has no language of its
  own: it is no longer shown or counted as "un". Its own language still
  takes one of the plan's languages.
- Tests: the vault key's file mode is checked only where POSIX modes
  exist.

### Upgrading

- To update from /admin from now on, once on the server as root:
  `curl -fsSL -o update.sh https://raw.githubusercontent.com/eullm/eullm-agent/main/deploy/server/update.sh && sudo sh update.sh install`
  (deploy/server/README.md, "Updating from the console").

## 0.20.1

### Added

- Editor Mode, "Il mio piano" at the bottom of the newsroom menu, with the
  plan's name and the days of trial left under it. Its page shows the plan,
  how much of it was used this month (the model's cost only to the
  operator), and for the owner the choice of the plan, the payment, the
  billing data and the cancellation; the forms and the payment come back
  there. /account stays, for the owners of several newsrooms.
- Editor Mode, Sito e profilo › "Fonti negli articoli pubblicati": a
  site can show its sources discreetly, with no numbers in the text and
  one line at the end naming each source once. The draft keeps its notes,
  only the published text changes. The default stays the notes.
- Guide "Preparare il proprio sito per Redazia" (editor-mode/docs):
  WordPress, static sites on GitHub, webhook, and a new site on a
  subdomain or under /news. The webhook now sends the article's section.
- An email that cannot be sent (invite, password reset) writes the reason
  in the log (`editor.mail`): before, only "not sent" was shown.
- The free trial is given once. At every signup from the site the
  customer's email (Gmail dots and +tags ignored), VAT number, tax code and
  phone, and the owner's email, are compared with every earlier customer;
  a site added to a newsroom on trial is compared with the sites of every
  other newsroom. Who comes back gets the newsroom on hold until a plan is
  paid, the site added is refused. Only hashes are kept (`trial_marks`),
  so a deleted newsroom still counts. /admin › Clienti lists the second
  trials refused and why; the operator can give the trial again in
  Abbonamento. The signup API answers `trial_used`.
- A signup for someone who already has an account: no password to choose,
  so the email says the newsroom is ready and where to sign in (before,
  no email at all). The API answers `owner.invite.existing`.

### Changed

- Editor Mode, Pubblicazioni: the form of a destination shows only the
  fields of the type chosen (WordPress, GitHub), in a grid with short
  labels on one line; the same layout when a destination is changed. The
  address of a GitHub destination shows the repository again, not the
  articles' address.
- The logo goes back to the Desk (to the console in Amministrazione); the
  account at the bottom of the menu is on two lines, the name and then
  role and "Esci".
- Articles carry their section: in the automatic newsroom the AI
  editor-in-chief files each article under one of the site's sections, a
  translation keeps its original's; the payload has `section` and a
  GitHub post gets it in its front matter (`section`, or the blog's own
  `category` key).
- The small settings on Sito e profilo (proposals a day, covers) and the
  text boxes of the automatic newsroom have the same look as the other
  fields.

## 0.20.0

### Added

- Editor Mode, automatic newsroom (Sito e profilo › Redazione automatica):
  a site that writes and publishes by itself. The owner gives a brief,
  the sections, directives, topics never to cover, articles a day, hours,
  the minimum Opportunity and the minimum number of different sources.
  Every hourly round, inside the hours, the day's articles are spread over
  the hours left: the best proposals that pass the guardrails are accepted
  (those on a topic to avoid are turned down) and each one goes to an
  `auto` job: draft with the usual checks, cover, then an AI
  editor-in-chief reads it against fixed rules (unconfirmed accusations,
  private people and minors, medical, legal or financial advice, hate,
  graphic violence, rumours as facts, misleading titles) and the
  directives. What passes is approved and published to the site's
  destination; what does not stays a draft with the reasons. "Ferma
  subito" stops it; the page lists the day's articles and how each ended.
- Every article of the automatic newsroom ends with the AI label in the
  site's language ("written by artificial intelligence from the sources
  cited, without human review", AI Act art. 50(4)), followed by a link to
  Redazia that the owner can turn off.
- Translations in the automatic newsroom: an article that passed is
  translated into the site's languages and each translation is published
  by itself, with its own label.
- Publishing destinations have a language: a translation goes to the
  destination of its language (for example the English folder of the same
  repository), the original to one without a language.
- Profile from the brief, for a new site with nothing to read yet: the
  model writes sector, audience, tone and one topic per section, with
  keywords in the site's language and in English; it is a version to
  approve like any other.
- The brief, the sections, the directives and the topics to avoid also go
  into the prompts of proposals and drafts, with or without the automatic
  mode.

## 0.19.0

### Added

- Editor Mode, publishing to a site's GitHub repository (Vercel, Netlify,
  GitHub Pages) as one of its own posts. Before writing, the newest posts
  of the articles folder are read and the new one copies them: front
  matter keys, order and date format (`pubDate: 'Jul 08 2026'`, `date:
  2026-07-08`...), the values every post shares (author, layout), `.md` or
  `.mdx` (braces and angle brackets escaped), one folder per post, the
  image as an address on the site or beside the post (`./cover.png`). The
  blog's list then shows the article and its thumbnail like the others.
- One commit per publication with the article, its cover, a line in the
  site's `llms.txt` and the article in `sitemap.xml` when the site keeps it
  as a file (a sitemap generated at the build updates itself): one deploy.
- Safe publishing: the commit goes on a pull request that is merged only
  when the site's checks pass (Vercel's or Netlify's preview deploy, read
  from commit statuses and check runs; up to 15 minutes, `wait_minutes`).
  A failed build leaves the site as it was, the pull request open and the
  link to the failed deploy in the publication. Without checks on the
  repository it is merged after 90 seconds. "Pubblica solo se il deploy di
  anteprima riesce" off commits straight on the site's branch.
- The publication's address is the article on the site, not the file on
  GitHub: learned from `llms.txt` or the sitemap, else the articles folder
  on the site's domain, or set as "Indirizzo degli articoli"
  (`https://site/blog/{slug}`). IndexNow gets that address.
- Cover images through OpenRouter's image API: a key starting with
  `sk-or-` draws with `openai/gpt-image-2` (quality `medium`, about 0.05 $;
  `EDITOR_IMAGES_QUALITY=low` under 0.02 $), any model of OpenRouter with
  `EDITOR_IMAGES_MODEL=owner/name`. A Black Forest Labs key still draws with
  FLUX. The image's real size is stored.
- Covers made to be clicked: each site chooses, in Sito e profilo, between
  "D'impatto" (the default: the story's most striking subject, dramatic
  light, strong contrast, saturated colours, readable as a thumbnail,
  never showing what the article does not say), optionally with a 2-4 word
  headline drawn in, and "Editoriali" (a plain news photo).
- `deploy/server/backup.sh` and [BACKUP.md](deploy/server/BACKUP.md):
  backup and restore of a server installation.

### Upgrading

- The GitHub token of a target needs, besides Contents, Pull requests
  (read and write) and, to read Vercel's result, Commit statuses (read);
  Checks (read) is optional. Without Pull requests, untick "Pubblica solo
  se il deploy di anteprima riesce" on the target.
- For OpenRouter covers put the key in `EDITOR_IMAGES_API_KEY`. A
  `compose.yaml` that sets `EDITOR_IMAGES_MODEL` to `flux-2-pro` keeps
  working: a FLUX name is replaced by the OpenRouter default.

## 0.18.3

### Security

- Dependencies with known advisories updated: teloxide 0.12 to 0.17 (it
  brought an old reqwest with rustls 0.21, rustls-webpki 0.101, h2 0.3 and
  webpki-roots 0.25), rustls 0.23.46, rustls-webpki 0.103.16, webpki-roots
  1.0.11, ctutils 0.4.6; certifi 2026.10.23 in Editor Mode. `cargo audit`
  and `pip-audit` report no vulnerability left (one unmaintained
  build-time crate, proc-macro-error2, used by teloxide's macros).
- Editor Mode behind a reverse proxy: the address it is reached at is
  taken from the proxy (`FORWARDED_ALLOW_IPS`, default every address, as
  the ports listen on 127.0.0.1 only), so over HTTPS the session cookie
  is `Secure` again.
- Every page carries security headers: no framing by other sites
  (clickjacking), `nosniff`, a referrer policy, a Content-Security-Policy,
  and HSTS over HTTPS.
- "Password dimenticata" sends one link per address every 10 minutes, so
  the form cannot flood a mailbox.
- The return address after an action refuses control characters
  (`/\t/evil.com` became `//evil.com` in browsers).
- Server guide: only Editor Mode goes on the internet; the Core stays on
  127.0.0.1 and is reached through an SSH tunnel.

## 0.18.2

### Fixed

- Editor Mode: the source search kept a CPU at 100% for hours. To judge
  how original a candidate source is, each of its items was signed again
  for every known signature (2000 and more, a few milliseconds each):
  about ten minutes per candidate, hours for a site. Each item is now
  signed once. The hourly collection signs only the new items, and the
  Opportunity score reads the words of the site's past articles once.

## 0.18.1

### Changed

- Editor Mode, Trend e piano: the editorial plan is one line per proposal
  (title; format, source, Opportunity and Hype) with the buttons on the
  right; the article, the news and the reasons open on a click. Proposals
  to decide come first, then the drafts being written. The explanation of
  the plan is folded under "Come funziona il piano".
- Accepting or discarding a proposal keeps the page where it was (the row,
  or the next one), on Trend e piano and on the Desk, instead of going back
  to the top.

## 0.18.0

### Changed

- Editor Mode: six proposals a day per site instead of three. The sites
  still on the old default (3) move to 6; a number someone chose stays.
  The number is set on the site's page, with one field, and applies at
  once (it is a setting, not a new profile version).

### Added

- Editor Mode, Sito e profilo: the site in numbers at the top of its page,
  each opening the page with the list: profile version and the one waiting
  for approval, sources active, candidate and suspended, news in 24 hours
  and 7 days, proposals to decide, made today, accepted and discarded,
  drafts to review, approved and discarded, publications (published,
  waiting, failed, destinations) and the model's cost in 30 days.

## 0.17.4

### Added

- Editor Mode: publish targets of kind GitHub, for the sites deployed from
  a repository (Vercel, GitHub Pages). An approved article is committed to
  the repository as Markdown with front matter and cover image, and the
  site publishes it at the deploy: a "draft" publication opens a pull
  request on a branch of its own, a "publish" one commits to the site's
  branch (configurable: repository, branch, articles folder, images
  folder). The credential is a GitHub token stored like every other one
  (encrypted in the dashboard or `EDITOR_SECRET_<TENANT>__<NAME>`); a send
  interrupted after the commit is safe to repeat, as a file already there
  with the same content counts as sent. `editor-mode/examples/import-editor-
  feed.yml` is a ready GitHub Action for the opposite direction: a site
  repository that pulls the approved articles from the site's feed.
  The cover's address in the front matter leaves out the folder the site
  serves as its root (`public/images/x.jpg` is `/images/x.jpg`).

## 0.17.3

### Fixed

- Editor Mode on the server: every read of the shared archive and every
  model call made for it (the hourly collection, the topic names) was
  refused by the Core, which takes tenant names that start with a letter
  or a digit, not `_archive`. Sites whose sources were read only by the
  archive (eullm.eu, rag-pro.com) never got a news item, the others only
  from collections started by hand, and topics kept names made of bare
  keywords. The archive is now `shared.archive` for the Core. This was
  also what suspended 46 sources this morning.

### Changed

- Editor Mode, Trend e piano: the topic map is four boxes (Scrivi ora,
  Nicchia utile, Rumore, Ignora) listing every topic of the week with its
  Opportunity and Hype, instead of dots whose labels overlapped.
- Each topic in the map shows the title of its latest news item (linked)
  and how many items and sources it has, five per box and then "e altri N".
  Topics still named by bare keywords are renamed by the model, ten more at
  every hourly collection, starting from the largest.

## 0.17.2

### Changed

- Editor Mode, richer articles: an article is at least 900 words (up to
  2000, the site's usual length in between; before it could be 400), with
  up to 12 more news items on the same subject as context, claims of one
  to three sentences and the details the items carry. A draft still far
  under its length is sent back once with the items it left out, and the
  longer version is kept only if it passes every check (Attività shows
  the step). The prompt now asks for a journalist's prose: varied
  sentences, no stock phrases, no em dashes, no closing summaries.

## 0.17.1

### Fixed

- Editor Mode: good feeds were suspended for everyone after five failed
  reads, which with hourly reads is five hours: a site down for an
  afternoon, or the server's own network, stopped TechCrunch, arXiv, Hacker
  News and the like for two weeks. Now an archive source is suspended after
  24 failed reads in a row (about a day) and a source suspended for failed
  reads is tried again every day, not after two weeks. When more than half
  of the sources fail in the same round the trouble is on this side (the
  server's network, DNS, the Core), so the failures are shown but do not
  count towards suspension, and Attività says so. Sources suspended by the
  old rule go back to work by themselves at the next collection.
- Editor Mode: "Accetta e scrivi la bozza" answered Internal Server Error,
  and so did every page with the Attività box while a draft was being
  written (the box read the proposals' sites the wrong way).

## 0.17.0

### Added

- Editor Mode, cover images. Each draft gets an image drawn for it by FLUX
  (Black Forest Labs, on its EU endpoint `api.eu.bfl.ai`), never taken
  from the news it comes from: the model writes the image prompt and the
  alternative text in the article's language (one short call), FLUX draws
  it (16:9, about one megapixel) and the image is kept in the database,
  since the provider deletes it after ten minutes. The draft page shows it
  with "Rifai l'immagine" and "Togli"; translations take the original's
  image with their own alternative text. At publication WordPress receives
  it in the media library, with its alternative text, as the post's
  featured image; webhooks get `image.url` (served at `/media/<token>`,
  without login, by an unguessable token). Each image is in Consumi with
  its cost (Black Forest Labs' credits, one cent each). Set
  `EDITOR_IMAGES_API_KEY` (and optionally `EDITOR_IMAGES_MODEL`, default
  `flux-2-pro`; `flux-2-klein-9b` costs less): without a key drafts have
  no image, as before.
- Consumi names the steps of translations and images.
- Editor Mode, delivery of the approved articles, for the customers whose
  site is not connected to a destination. The draft page has a "Consegna"
  card: "Scarica il pacchetto" gives a ZIP with the page ready to open
  (`article.html`, with the search fields, the schema.org data and the
  image), the text alone to paste into any CMS (`body.html`), Markdown
  with front matter for Hugo, Jekyll and Astro (`article.md`), the cover
  image and every field for Google (`meta.json`); "Copia HTML" and "Copia
  Markdown" copy the text without its title. At each approval the
  newsroom's email recipients (Impostazioni) get the article in the
  message with the package attached, a job of its own shown in Attività
  (needs SMTP). In Sito e profilo › Come ricevi gli articoli the owner
  creates the site's feed, JSON Feed 1.1 and RSS 2.0, with the last 50
  approved articles complete with text, image and search fields
  (`?lang=en` for one language), for Zapier, Make, n8n or a site that
  imports a feed: its address holds an unguessable token, needs no login,
  and can be changed. API: `GET /api/drafts/{id}/package` and
  `POST /api/sites/{id}/feed`.

## 0.16.0

### Added

- Editor Mode, translations. In Sito e profilo › Lingue degli articoli the
  owner picks the languages a site's articles are translated into
  (English, Italian, German, French, Spanish, Portuguese, Dutch, Polish),
  within the plan's languages per site (`max_languages`, its own included;
  now on the newsroom too, copied from the plan, set in /admin and with
  `editor tenant-plan --max-languages`). When an article is approved the
  agent translates it into each of them, one model call per language: a
  translation is a draft of its own with the same sentences in the same
  order and the same sources, checked like the original (numbers found in
  the sources, nothing copied), with its own search title, description and
  slug, approved and published separately. Each translation counts as one
  article of the plan. A draft page lists the original and its
  translations, with "Traduci" for a language still missing. API:
  `PUT /api/sites/{id}/languages`, `POST /api/drafts/{id}/translations`;
  approving a draft through the API answers with the languages being
  translated.

## 0.15.0

### Added

- Editor Mode, the news base: 46 general news feeds read once an hour in the
  shared archive for every newsroom, whatever its sites follow: world and
  politics, economy and technology, in English (BBC, Guardian, NYT, Al
  Jazeera, DW, France 24, Euronews, FT, Ars Technica, The Verge, TechCrunch,
  Hacker News...) and in Italian, German, French and Spanish (ANSA,
  Corriere, Repubblica, Il Sole 24 Ore, tagesschau, Spiegel, heise, Le
  Monde, El País, Xataka...), so national news reach the sites of that
  country. `EDITOR_BASE_NEWS=0` turns them off.
- News index (/index): every three hours from 07:00, the 40 topics rising
  most across all sources, ranked, each with a short summary, its news,
  how many sources tell it and its section (world, economy, technology, or
  the sites' own sources), with tabs per section and "Titoli per <sito>".

### Changed

- The keywords of a site's profile are written in the site's language and
  again in English, so both national and international news are found.
  Existing profiles get them at the next analysis ("Rianalizza").
- Topic labels by the model are capped at the 10 largest topics touched by
  each run, so the larger base does not multiply the cost; the others keep
  their keyword labels until they grow.

### Fixed

- Trend e piano showed "<built-in method items>" instead of the number of
  news of a topic.

## 0.14.0

### Changed

- Editor Mode, proposals every day. Until now a proposal needed a topic
  told by two sources, or one "strong" news item, and its closeness to the
  site was a word-for-word match between the profile's keywords, in the
  site's language, and news that come mostly in English: Italian sites got
  "nothing to propose" in 0 seconds. Now the topics come first, as before,
  and the rest of the day's number comes from the best single news items of
  the last 72 hours for the site (its own sources first, then the shared
  archive): scored without the model on word roots (so "inferenza" meets
  "inference"), the site's sources and their rating, freshness, how many
  sources tell the story and what the site already covered. One model call
  reads the first 30 and picks the best, writing title and angle (before:
  one call per proposal); without the model the formula's first ones are
  taken. Each such proposal is marked "notizia singola" and says why it was
  chosen. Nothing is proposed only when no news came in for 72 hours, and
  the Attività box says so in red.
- Desk, a cockpit of the whole newsroom: six figures at the top (decisions,
  proposals, drafts, news in 24 hours, sources, model cost over 30 days),
  decisions one line each with the details on click, the Attività box one
  line per job with the full day on the new Attività page (/activity), the
  sites table with each site's model cost over 30 days, and the most active
  sites, the most expensive sites and the latest news.

### Added

- `proposals.basis`: what a proposal stands on (a topic, or a single news
  item with its score and who picked it). Migration 0023 (SQLite 0012).

## 0.13.1

### Added

- Server compose: the Core receives `ANTHROPIC_API_KEY`, for a Claude model
  (`type: anthropic` in core.yaml).

### Fixed

- Console, "Consumi" page: the "Esci" (log out) button was missing.

## 0.13.0

### Added

- Editor Mode, model usage measured call by call. Every model call is
  recorded when it happens, also when the work then fails: newsroom, site,
  article (the proposal or topic it was for), step, the model that
  answered, input and output tokens, duration, the job it ran in and, for
  a failed call, the error. Before, calls were recorded only at the end of
  a successful step, without site or article.
- Console, "Consumi" page (/admin/usage): tokens, calls, errors, average
  duration and cost per step, model, newsroom and site over 1, 7, 30 or 90
  days; the cost of every article written (writing attempts and the
  proposal of its topic) and the average per article; every call as CSV.
- Reference prices per model (euro per million input and output tokens),
  set in the console: the cost of an article is estimated also when the
  model is free today.

### Fixed

- A newsroom with a timezone the system does not know no longer breaks the
  console: it counts as UTC.

### Upgrading

- Migration 0022 (Postgres) / 0011 (SQLite) adds the new columns of
  `llm_usage` and the `model_prices` table; it runs with the update.

## 0.12.3

### Fixed

- Editor Mode, site page: two separate statuses instead of one that looked
  contradictory. "Lettura del sito" says how much of the site was read
  (articles, dates, language); "Profilo" says whether the model worked out
  sector, audience and style. Before, a site could show "Analisi completa"
  with a partial profile, or "Analisi parziale" with a complete one.
- A model provider that fails for a moment (5xx, rate limit, timeout) is
  asked again after 15 and 45 seconds, for every model call (profile,
  proposals, titles, drafts, sources); Activity says so. Refusals such as a
  spending limit or an invalid answer are not retried.
- Work stopped by a model provider that stays down (a draft, for example)
  is no longer marked failed: it waits and runs again by itself after 10,
  20, 30... minutes, up to 8 attempts, and Activity shows when.
- Editor Mode waits up to 15 minutes for the Core (`EDITOR_CORE_TIMEOUT`,
  in seconds) instead of 3: a model running on the same machine, or a
  spare model tried after the first, has the time to answer.
- A profile left incomplete because the model did not answer is analysed
  again by the hourly tick, waiting longer each time (up to 6 times a
  day), with no button to press. A review of an approved site where the
  model does not answer keeps the approved line and proposes nothing,
  instead of a version without sector, audience and style.

## 0.12.2

### Added

- Editor Mode: whatever is created from the dashboard can now be corrected
  or deleted from it, without a command line.
  - Sites: rename; correct a mistyped domain until a profile is approved
    (the site is analysed again under the new domain); delete a site with
    everything that belongs to it, typing its domain to confirm. Work queued
    for it is dropped; a site with work running is deleted when it ends.
  - Publishing targets: edit name, address, WordPress user, IndexNow key,
    JSON-LD and on/off; delete a target (articles already on the site stay).
  - Briefing recipients: edit; newsroom preferences (timezone, briefing
    hour, language) set by the owner in Impostazioni.
  - Drafts: edit title, subtitle, every sentence and the SEO fields; claims
    are checked again and an approved draft goes back to review. Delete a
    draft; its proposal can be written again.
- Console: rename a newsroom; edit and delete customers (not while they
  have a newsroom) and resellers (not while they have customers); add and
  delete plans (not the trial plan, not a plan in use); remove an operator
  (never yourself, never the last one); revoke an operator invite.
- Translations of the new screens in English, German, French and Spanish.

## 0.12.1

### Fixed

- Editor Mode: a site added from the dashboard is listed at once, in the
  sidebar and on the Desk, while its analysis waits or runs; before it
  appeared only when the analysis ended.
- Activity: an analysis that could not read the site shows as failed, in
  red, with the reason (for example a domain that does not answer, not even
  robots.txt), instead of "Done". A profile the model could not complete
  says so ("Da completare") and why; a search for sources before the
  profile is approved says that sources come after the approval.
- A newsroom runs two jobs at a time instead of one, so a second site's
  analysis no longer waits minutes behind the first one's model step; the
  same work on the same subject, and two collections, never run together.
  The model step says it usually takes 1-3 minutes, and it is skipped when
  the site could not be read.

## 0.12.0

### Added

- Editor Mode, the Desk (it replaces "Oggi"): every site on one page. A
  table gives each site its profile, active sources, news in the last 24
  hours, proposals and drafts waiting, the next step it needs and buttons
  to analyse it, find sources and propose articles; the collection runs
  for all sites at once. Decisions of every site are listed together.
- Activity: every job started from the dashboard shows at once, queued,
  then running with the step it is on ("Lettura degli articoli del sito:
  7 di 25", "Lettura delle fonti: 12 di 40", "Il modello scrive la
  bozza"), then what it produced with a link to it: a profile to approve
  or "no change", sources tried and activated, news and topics read,
  proposals made, the draft written. A failed job says why and at which
  step. The box refreshes by itself on every page and the page reloads
  when the work ends; if work waits more than three minutes with nothing
  running, it says the worker seems stopped.
- When no proposal comes out, the result names the topic closest to one,
  with its Opportunity and its news and sources. An accepted proposal
  shows whether its draft is being written or failed, and why.
- Jobs keep their step and their result (`editor.jobs.progress`,
  `editor.jobs.result`, migration 0020).

## 0.11.0

### Added

- Editor Mode, articles written for search engines and AI answers: the
  first lines answer the reader's main question; the headings are the
  questions people search for; two to four frequently asked questions
  close the article, each answered with sourced claims checked like the
  rest; a key phrase is chosen and used in the title and the first
  sentence; a "Read more" section links up to three of the site's own
  articles on the same subject.
- Every draft carries its search fields (key phrase, a title for Google
  within 60 characters, a description within 160, the address words)
  and a checklist on its page, "Google and AI answers", that says what is
  in place and what is missing.
- Publishing: WordPress gets the address words and the description as the
  excerpt; with the new option, the post also carries schema.org JSON-LD
  (NewsArticle with its sources, FAQPage) that says what the page says.
  The webhook sends the same fields. With an IndexNow key on the target,
  every published article is reported at once to Bing and the engines
  that share IndexNow (the key file goes at the site's root); a failed
  report is logged and the article stays published. Google has no such
  call for articles: it finds them through the sitemap and internal links.

## 0.10.1

### Added

- Editor Mode: "Lancia adesso" on the home page runs every step on
  request, without waiting for the schedule: analyse the site, find
  sources, collect the news (then topics, Hype, a fresh trend of the
  moment and Opportunity) and propose articles. Proposals asked for by a
  person are no longer held back by the day's number; the automatic ones
  still are.

### Changed

- CI: main is not checked again after a merge (the branch merged was
  checked by its own push); only the release cache is built there. The
  release page is created as soon as the tag is pushed and each platform
  adds its files when its build ends, so a queued Windows or macOS runner
  holds up nothing. Every job has a time limit: a hung step fails in
  minutes instead of running for hours.

## 0.10.0

### Added

- Editor Mode, shared archive: every public source is read once for all
  newsrooms, not once per newsroom. Items, topics, topic labels and Hype
  Scores live in a reserved tenant, `_archive`, that newsrooms can read
  but not change (row level security, read-only policies); the hourly
  collection reads only the sources at least one newsroom follows. Each
  newsroom keeps its own choices: which sources each site follows and
  their status, the site's rating of each source, Opportunity, proposals,
  drafts and feedback. A newsroom that starts following a source already
  in the archive sees its history at once. A source that stops answering
  is suspended in the archive for everyone; a source off topic is
  suspended only for the site that follows it. The archive's pruning
  keeps every item a newsroom cites. On SQLite (local installation) the
  only newsroom is its own archive. The migration moves existing sources
  into the archive without duplicates and keeps them followed; items and
  topics collected so far stay where they are and age out.
- The trend of the day: from 07:00, every three hours (eight times a
  day), the archive keeps the topics rising most with their latest news.
  "Trend e piano" opens with the trend of the moment for the site: the
  rising topics close to its profile, their sources and news, and the
  titles proposed for each, with a short summary and "Scrivi questo" to
  have the draft written. "Proponimi tre titoli" asks the model for three
  different articles on the same trend. The day's snapshots, hour by
  hour, are listed below.
- GitHub Trending: GitHub sources read the Trending page (repositories
  developers are starring today, with stars gained today) instead of
  word searches, which found new repositories nobody used. A newsroom's
  automatic GitHub searches are suspended and replaced by GitHub Trending
  for the same site; a status a person chose stays as it is. A trending
  repository's summary says its rank and the stars gained, and its stars
  of the day count towards the topic's Hype.

### Changed

- Drafts are richer: besides the proposal's citations, the model gets
  other recent news on the same subject as context, and a target length
  taken from the articles of the site.
- Proposals use the site's own rating and name of a source, not the
  archive's.
- The daily items limit of a plan counts the new items of the sources a
  newsroom follows; it no longer stops the shared collection.

## 0.9.3

### Fixed

- Core `/v1/fetch` took a "304 Not Modified" for a redirect and failed
  with "Redirect without Location". Every feed answering a conditional
  GET (ETag, Last-Modified) failed from its second read on, so Editor
  Mode suspended most sources after a few hours and collected almost
  nothing. Only 301, 302, 303, 307 and 308 are followed now; sources
  suspended by this error go back to work at the next collection.
- Editor Mode: a fetch the Core could not make (the newsroom used up its
  daily fetches, or the Core was down) no longer counts as a failure of
  the source. Before, a night past the limit suspended every source for
  "failed fetches in a row". Sources suspended that way go back to work
  at the next collection; a status set by a person stays as it is.

### Added

- The home page says when the last round could not read sources because
  the daily fetches ran out or the Core did not answer; the Fonti page
  shows each source's last read error.

### Changed

- `deploy/server/core.example.yaml`: the default daily fetches of a
  newsroom go from 2000 to 10000. Hourly collection of 60 sources is
  already about 1500 a day.

## 0.9.2

### Changed

- Editor Mode: every proposal, on the home page and in "Trend e piano",
  says in plain words what it is: the proposed article and its format,
  the news it comes from (link, source and date), why it suits the site
  (the editorial profile subject it falls under, and why now when the
  model wrote it) and what its two scores mean. A low Hype from a single
  source is explained, not left as a bare "Hype 0".
- A proposal made without the model takes the newest headline as its
  title instead of the topic's keywords.

## 0.9.1

### Added

- Editor Mode: every page opens with "Come funziona questa pagina": what
  it does, what to expect and what to check, in the five languages; it
  can be closed and the browser remembers it. A new "Guida" page lists
  the five decisions left to people and every step of the newsroom, from
  the domain to the published article.

### Changed

- The language selector is at the top right of every page.

## 0.9.0

### Added

- The Editor Mode dashboard, the account page and the console in five
  languages: English, Italian, German, French and Spanish. The language
  comes from the selector at the bottom of every page (kept a year in a
  cookie), else from the browser, else `EDITOR_LANGUAGE` (English by
  default). Notices, errors, the invite and password emails follow it;
  the trial reminder follows the newsroom's language.
- Signup from the public site: `"language"` in the body (or
  `Accept-Language`) sets the language of the error messages, of the
  owner's invite and of the newsroom.
- Signup from the public site: the plan the visitor picked is kept as
  `desired_plan`/`desired_interval` while the newsroom is on trial, and is
  already selected in `/account`. A paid plan never starts without its
  payment from the site's key (it did before when `plan` was sent).
  A payment recorded by bank transfer clears the wish too.

### Fixed

- Local installation (SQLite): the queue no longer runs the work of a
  suspended newsroom.
- Core: the delegate limits of a token whose tenant is itself nested
  (`parent/child`) apply to its sub-tenants.
- Account page: billing data for a newsroom that does not exist give a
  message instead of an error.
- A payment recorded outside the gateway (bank transfer) cancels the
  automatic renewals at Mollie, so the customer is not charged twice. If
  Mollie does not confirm the cancellation, or online payment is not
  configured, nothing is recorded and the console says why, instead of
  leaving the renewals running.

### Upgrading from 0.8.3

- Migration 0016 adds two columns to `editor.subscriptions`; it runs by
  itself when the containers start.
## 0.8.3

### Changed

- A proposal can also come from one strong news item: a topic with an
  opportunity of at least 50/100 from a source scored 0.60 or more. Two
  items from two different sources still make a story at any score.
- The day's proposals no longer stop at the first topics that fail the
  rules: the next ones are tried until the day's number is reached.

### Added

- "Proponi adesso" in Trend e piano: the day's proposals now, without
  waiting for the briefing, up to the number a day set in the profile.
- Sources page: how many news items each source brought in the last 7 days.
## 0.8.2

### Fixed

- A proposal needs at least two news items from two different sources: the
  same repository found by two GitHub searches was enough before, and gave
  topics like a namesake repository.
- Editor home page, "Opportunità del giorno": only topics that could become
  a proposal (two items from two sources), each with how many items and
  sources it has.
- A source the daily maintenance suspended is no longer counted in "Da
  decidere": it is listed under Raccolta, saying nothing has to be done.
- "Primi passi": after discarding proposals the step says so and when the
  next ones come; the proposal button reads "Accetta e scrivi la bozza",
  because accepting already writes the draft.
## 0.8.1

### Fixed

- Editor home page: "Apri il registro" under a suspended source opened the
  sources of the site in view, which could be another site, so the list
  was empty. It now switches to the source's own site first, and the
  suspended source shows which site it belongs to.
- Sources page: when the site in view has no source in the chosen state,
  it names the other sites that do, with a link to each.
- "Primi passi" counts the sources and news of the site in view, like the
  Fonti page; the "fonti attive" count under Raccolta says when it covers
  every site.
## 0.8.0

### Added

- Selling the service from the backoffice, ready for a public site and
  for resellers:
  - **Plans** in the console (`/admin` → Piani): prices without VAT,
    monthly and yearly, trial days, limits (sites, languages per site,
    active sources, items a day, articles a month). The provisional
    catalogue is Prova (7 days), Starter 49 €, Pro 149 €, Business 399 €.
    The public site reads it from `GET /api/public/plans`, without a key.
  - **Customers**: the billing data of whoever pays (company or person,
    VAT number, tax code, address, SDI code, PEC, checked), sent by the
    site with `POST /api/operator/signup` together with the newsroom on
    trial and its owner's invite; `GET`/`PATCH /api/operator/customers`.
  - **Trial and its end**: an email to the owner two days before; when the
    trial (or a paid period, after three days of grace) ends, the newsroom
    stops but keeps its data, and its owner signs in to `/account` to
    choose a plan. A banner in the newsroom counts the days left.
  - **Payments**: with `EDITOR_MOLLIE_API_KEY` the owner pays from
    `/account` (card, PayPal and the other Mollie methods) and the
    renewals are automatic; Mollie's notices arrive at
    `/api/billing/mollie/webhook`. Without it, bank transfers are recorded
    from the console or with `PUT /api/operator/tenants/{id}/subscription`.
    The gateway sits behind a small interface, so another can replace it.
  - **Resellers** (`/admin` → Rivenditori): an API key bound to the
    reseller signs up and manages only its own customers, which it bills
    itself.
  - The console lists customers with their billing data and newsrooms, and
    every newsroom shows its plan, state and paid period.
- "Primi passi" on the home page: the way from the domain to the first
  published article, step by step, with what the newsroom is waiting for
  (the scan, the sources, the first collection, the morning proposals) and
  what is up to you. It goes away once every step is done.

### Upgrading from 0.7.7

The database migration runs by itself. Newsrooms that already exist have
no subscription and keep working as before; give one a plan from the
console when it should be billed. For online payments add
`EDITOR_MOLLIE_API_KEY` to `.env` (and `EDITOR_PUBLIC_URL`, which Mollie
needs to reach the server), and to a `compose.yaml` written by an earlier
`server-init` the line `EDITOR_MOLLIE_API_KEY: ${EDITOR_MOLLIE_API_KEY:-}`
under `x-editor-env`.

## 0.7.7

### Added

- A spare model for the main one: `fallback:` at the top of the
  configuration names a model of `models` the Core uses when the provider
  fails (an error, a rate limit, a timeout); a spare can have its own. The
  server's `core.example.yaml` has a commented example with Mistral and
  OpenRouter's free models, and `compose.yaml` passes their keys
  (`LLM_FALLBACK_API_KEY`, `LLM_FALLBACK2_API_KEY`).
- `/v1/llm/chat` answers with `served_model`, the provider's model that
  answered. The dashboard shows it bottom left, instead of "default".

### Fixed

- A newer re-analysis replaces the proposal of an older one still waiting:
  two versions to approve (v2 and v3) no longer pile up.
- A topic taken out of a proposal stays out after the next analysis too.

### Changed

- "Aggiungi un dominio" moved from the bottom of the profile page to
  Impostazioni ("Siti della redazione"), with a link under the site picker.
  The profile page keeps it only when there is no site yet.
- When a proposal is compared with the approved profile, the topics that
  leave are listed in the comparison only, not again in the topics table.

### Upgrading from 0.7.6

- To use a spare model on an existing server, add `LLM_FALLBACK_API_KEY`
  (and `LLM_FALLBACK2_API_KEY`) under `core:` → `environment:` in your
  `compose.yaml`, as in `deploy/server/compose.yaml`, and the `fallback:`
  block to `core.yaml`. Nothing to do otherwise.

## 0.7.6

### Fixed

- A proposal no longer rests on a single item: a topic needs at least two
  before the plan proposes an article on it. A new repository with no
  stars, found because it shares a name with the site's product, had
  become a proposal on its own.
- GitHub searches found by source discovery keep only repositories with at
  least 10 stars; the ones already found get the same filter at the next
  source maintenance. A repository's title now carries its description
  ("owner/name: what it is"), so topics made of repositories have a
  readable name.

### Changed

- The editorial plan says what accepting means (the idea goes in the plan,
  then "Scrivi la bozza" writes the article), what Opportunity and Hype
  measure, both out of 100, and the format in Italian.
- Release: the server's Core image is made from the Linux x64 binary the
  release has just built, instead of compiling everything again inside
  Docker. The release build starts from main's compiled dependencies (a new
  CI job keeps them cached on main). A pull request from a branch of this
  repository no longer runs CI twice: its push already did.

## 0.7.5

### Added

- Topics can be taken out of a site's line ("Togli il tema"): they are no
  longer followed and later analyses do not propose them again. "Rimetti"
  brings one back. On a draft the change is made in place; on an approved
  profile it becomes a new approved version.
- Profile versions that are not in use can be deleted from the Versions
  table. The approved one cannot, nor one that already produced proposals
  and scores.

### Fixed

- A site without an RSS or Atom feed, or without a sitemap, is no longer
  called partial for that alone: the articles are found anyway. These are
  shown as notes.
- The reasons of an incomplete analysis, and why a source was suspended,
  are shown in Italian.
- Hacker News searches found by source discovery followed the most popular
  stories of all time, so they looked silent and were suspended after 45
  days. They now follow the newest stories; the ones already suspended for
  this are moved and reactivated by the next source maintenance.

### Changed

- A proposed profile says what changes against the approved one, row by
  row: sector, audience, tone, languages, each topic (new, leaving, share
  before and after), and why topics found on every page leave.
- Home: a Sites card lists every domain with the version in use, and the
  one to approve highlighted only when there is one. Profiles to approve
  are listed by domain and version.
- The sidebar shows the Editor Mode version and the model last used, in
  small print. Editor Mode's own version now follows the release
  (`editor-mode/pyproject.toml`, was 0.1.0).
- Calmer colours: a suspended source is shown in teal; red is kept for
  errors and blocking problems.

## 0.7.4

### Fixed

- Analysing a site again now proposes an updated profile when the analysis
  method has improved or the new reading finds what the old one missed
  (the sector), even if a profile is already approved. Before, an approved
  profile stayed as it was until the site itself changed.
- Source discovery keeps only the feeds published on the source's own
  domain (euractiv.com no longer brought in another site's feed).

### Changed

- The profile page explains the editorial line in plain words: languages,
  pace, length, topics with their keywords and two example articles each,
  and what approving means step by step. When the analysis is incomplete
  the main button is "Rianalizza", and approving stays possible.
- The sources page says how each source was found (cited by the site's
  articles, found by searching a term on Hacker News, GitHub, arXiv or
  Hugging Face, or suggested by the model and verified), shows its latest
  news, the reason a source was rejected and how its score is made up.
- More sources per site: one search per topic as well as the model's
  terms (up to 8), and up to 15 domains suggested by the model, also in
  the site's languages.

## 0.7.3

### Fixed

- A domain that redirects to another host (rag-pro.eu to rag.i3k.eu) is
  read where it lands: before, its sitemap, links and articles were all
  discarded as another site's, and the analysis found nothing.
- A site analysed again before its profile was approved showed two
  profiles to approve on the home page: the newer analysis now replaces
  the draft still waiting.
- Work in progress names the site by its domain ("Ricerca fonti
  (i3k.eu)"), not by its number.

### Changed

- The profile page says what you approve: a summary of the editorial line
  written by the model from the pages read, and what the agent will do
  with it (which sources it looks for, which news it keeps, how trends are
  scored, how many proposals a day, in which language and tone the drafts
  are written). An incomplete analysis says so before you approve it.

## 0.7.2

### Fixed

- Site analysis: keywords found on every page (a site-wide meta keywords,
  or a tag put on everything) are no longer taken for subtopics, which all
  showed 100%. They are kept as the site's keywords and given to the model.
  Each article now counts for one subtopic only, in the analysis without
  the model too.
- The analysis reads up to 25 articles (was 10, fewer than the 15 it asks
  for to call itself complete) within 60 pages (was 40).
- When the model's part fails, the profile says why.

### Added

- Languages: each article's language is read, the profile shows the
  articles per language ("Lingue: it (12), en (11)") and how many are
  translations of each other (hreflang), and the main language is the one
  most articles are in.

## 0.7.1

### Changed

- **The administrator is created at installation.** `server-init` asks
  for your email and password (typed twice, not shown, written nowhere)
  and the name of a first newsroom, and once the stack is up creates the
  administrator account and the newsroom, owned by it: you sign in at
  `/login` and land on `/admin`; `/editor` opens the newsroom. Without
  questions: `--admin-email`, `--newsroom` and `EDITOR_ADMIN_PASSWORD`.
- With `docker compose up -d` alone, `EDITOR_ADMIN_EMAIL`,
  `EDITOR_ADMIN_PASSWORD` and optionally `EDITOR_FIRST_NEWSROOM` in `.env`
  do the same at the first start. They are used only while no
  administrator exists; empty, the first-start link of 0.7.0 is printed.
- New `editor admin-init <email> [--newsroom NAME] [--password-stdin]`,
  which `server-init` runs. With an administrator already there it
  changes nothing.

## 0.7.0

### Changed

- **First start from the browser.** `docker compose up -d` starts
  everything; while there is no administrator, the start prints a
  single-use link (`docker compose logs editor-migrate | grep setup`, or
  `editor setup-link` for a new one) where you choose the email and
  password of the administrator. The login page says how to find it.
- **`/admin` and `/editor`.** The administration (formerly `/operator`,
  which now redirects) is at `/admin`: an administrator lands there after
  signing in. `/editor` opens the newsroom (an administrator's first one;
  "Entra" in `/admin` opens any other).
- `server-init` asks only for the model and its key: it no longer creates
  a newsroom or an account, it prints the first-start link. The options
  `--tenant` and `--email` are gone; newsrooms are created from `/admin`.

### Upgrading from 0.6.x

Nothing to do if you already have an administrator account: sign in and
you land on `/admin`. Without one, after updating run
`docker compose exec editor-web editor setup-link` and open the link.

## 0.6.1

### Fixed

- `editor user-add --tenant` with an id that does not exist stops with
  "no newsroom …" and the list of the newsrooms, instead of a database
  error after creating the account halfway (its password never shown).
  Account, password and membership are now written together.

## 0.6.0

### Added

- Editor Mode: **accounts**. People sign in with email and password
  (stored as scrypt hashes; five wrong passwords lock the account for 15
  minutes) and may belong to several newsrooms, each with its role. An
  owner invites people from Impostazioni (lettura, editor, owner), changes
  their role or removes them: a removed person is out at once. Invites and
  password resets are single-use links, sent by email when SMTP is set and
  shown otherwise. Tokens remain for integrations.
- Editor Mode: **operator console** (`/operator`, for operator accounts):
  every newsroom with plan, limits and usage; create one and invite its
  owner, change plan and limits, suspend and reactivate, delete; enter a
  newsroom as owner; invite other operators; password-reset links; API
  keys for the billing system.
- Editor Mode: **provisioning API** (`/api/operator/tenants`, with an
  operator key `edo_...`): create a newsroom when a payment goes through
  (its owner gets the invite), read them with their usage, change plan and
  limits, suspend (payment failed), invite, delete. An `external_ref` (the
  subscription) makes a repeated notice return the same newsroom.
- `editor user-add` (account, optionally operator and member of a
  newsroom; prints a generated password) and `editor operator-key`.
- `server-init` asks for your email and creates your account, operator of
  the service and owner of the first newsroom, instead of a login token.
  It asks the newsroom's name (the id is derived from it) and does not
  show the model's key while it is typed.
- Editor Mode: the EuLLM logo in the dashboard.

### Fixed

- Editor Mode on a server: `editor tenant-delete` deleted nothing and
  failed, because row-level security applies to the schema owner too
  (it is not a superuser there). It now deletes in the tenant's scope.
- Editor Mode: the site page said "Nessun sito" while the first analysis
  was still queued or running, or had failed: it now shows the work and
  its error before the site exists.

### Upgrading from 0.5.4

In the installation folder (`~/eullm-server`):

    sed -i 's/^EULLM_VERSION=.*/EULLM_VERSION=0.6.0/' .env
    docker compose pull && docker compose up -d

The migration adds the account tables. Then create your account, operator
of the service and owner of your newsroom (the password is printed once);
the first command shows the newsroom's id:

    docker compose exec -T postgres psql -U postgres -d eullm -Atc "SELECT tenant_id, name FROM editor.tenants"
    docker compose exec editor-web editor user-add you@example.com --operator --tenant <id> --role owner

and sign in with email and password. Existing tokens keep working. For
invite links that point to the public address of the dashboard, add
`EDITOR_PUBLIC_URL: ${EDITOR_PUBLIC_URL:-}` under `x-editor-env` in
`compose.yaml` (server-init does not rewrite an existing file) and set
`EDITOR_PUBLIC_URL` in `.env`; set SMTP to send them by email.

## 0.5.4

### Added

- `eullm-agent server-init`: the server installation set up and started in
  one command, with no checkout of the sources. It writes what is missing
  in its folder (compose file, `core.yaml` for the model chosen, `.env`
  with every password, token and key generated and readable only by its
  owner), starts the stack with Docker and creates the first newsroom,
  printing its owner's login once. Run again, it changes no existing file.
- Every release publishes the images of the Core
  (`ghcr.io/eullm/eullm-agent`) and of Editor Mode
  (`ghcr.io/eullm/eullm-agent-editor`), Linux x86-64. `deploy/server`
  pulls them (`EULLM_VERSION` in `.env`, `latest` when empty) instead of
  compiling; `docker compose up -d --build` still builds from the checkout.

### Fixed

- Editor Mode: subtopic shares count each article once, for the subtopic
  it matches best, and words found in every article (the site's own name)
  weigh nothing: shares no longer read 100% for several subtopics.
- Editor Mode: approving a profile retires the older drafts still
  waiting, which no longer ask for a decision.

### Upgrading from 0.5.3

`git pull`, then in `.env` add `EULLM_VERSION=0.5.4` and run
`docker compose pull && docker compose up -d`. The first time, the two
images must be made public on GitHub (organisation eullm → Packages →
each image → Package settings → Change visibility); until then
`docker compose up -d --build` keeps building them on the server.

## 0.5.3

### Fixed

- **Core on a server: it stopped starting after the first restart.** Its
  database role is named `core` like its schema, so once the schema
  existed the migration history was looked for there, not found, and every
  migration applied again ("relation runs already exists"). The history is
  now always read from schema `public`. A server already stuck needs, once:
  `docker compose exec postgres psql -U postgres eullm -c "ALTER ROLE core
  SET search_path = public"`, or this version.
- Editor Mode: when robots.txt cannot be read, the site page now shows why
  (for example the Core's answer), not only the error type.

The v0.5.3 tag still reports version 0.5.2.

## 0.5.2

### Added

- `provider.max_tokens` for `type: openai`: the output tokens per answer
  sent to the service. Reasoning models (for example on NVIDIA Build)
  count their reasoning in it, and with the service default the answer
  can come back empty. Without it nothing is sent, as before.

## 0.5.1

### Fixed

- **Editor Mode on a server: queued work never started.** The queue
  functions run as the schema owner, and row level security on
  `editor.jobs` applied to the owner too: where the owner is not a
  superuser, as in `deploy/server`, the worker found no work and site
  analyses stayed "in coda". Migration 0013 lifts it for the owner only;
  the application role is still filtered. The local installation
  (SQLite) was not affected.
- The worker logs every job it starts and finishes, with its duration.

### Upgrading from 0.5.0

`git pull && docker compose up -d --build`: the migration runs at start
and the work already queued starts by itself.

## 0.5.0

Editor Mode ready for many newsrooms on one server: durable queued work
served in turn, every newsroom a Core tenant of its own with its own
budget, publishing credentials entered from the dashboard and stored
encrypted, and a newsroom's data exported or deleted whole.

### Added

- **Sub-tenants for applications with many customers.** A Core token with
  `delegate: true` may add `X-EuLLM-Tenant: <name>` to any request and act
  as tenant `<tenant>/<name>`: its own runs, approvals, usage, audit and
  limits. `PUT /v1/tenants/{name}/limits` sets a sub-tenant's limits,
  stored in the database (migration 0004, SQLite 0002) and effective at
  once, with no change to the configuration and no restart;
  `delegate_limits` applies to a sub-tenant that has none. Limits in
  `api.tenants` still take precedence. Any other token is refused with the
  header, and a sub-tenant cannot set limits.
- Editor Mode uses it with `EDITOR_CORE_DELEGATE=true`: each tenant's model
  calls and fetches run as its own Core tenant, and `editor tenant-plan
  --max-cost-month` also sets that tenant's monthly budget in the Core,
  where calls under way are reserved. `deploy/server` is configured this
  way. A tenant with its own token (`--core-token-env`) keeps using it.

- **Editor Mode: publishing credentials from the dashboard.** The owner
  of a tenant enters the WordPress application password, the webhook key
  or the Telegram bot token when adding a target (page Pubblicazioni, or
  `PUT /api/secrets/{name}`); it is stored encrypted with AES-256-GCM in
  `editor.secrets`, bound to its tenant and name, and never shown again,
  only replaced or deleted. The key is `EDITOR_SECRET_KEY` (`editor
  secret-key` prints one); the local installation creates its own key
  file next to the database. A credential in the database comes before the
  `EDITOR_SECRET_<TENANT>__<NAME>` variable, which keeps working.
- The dashboard asks for the WordPress user of a target, which it did not
  before, so a WordPress target added from the dashboard could not
  publish.

- **Editor Mode: a tenant's data, whole.** `editor tenant-export` writes
  every row of a tenant as JSON, credentials left out; the owner downloads
  the same file from Impostazioni (`GET /api/export`). `editor
  tenant-delete <tenant> --confirm <tenant>` deletes a tenant and all its
  data in one transaction. The daily maintenance deletes collected items,
  their observations and finished jobs older than `EDITOR_RETENTION_DAYS`
  (default 365, `0` keeps everything); items cited by a proposal or a
  draft and model usage are kept.

### Changed

- **Editor Mode: durable job queue, fair between tenants.** Site analyses,
  source discoveries, drafts and publications asked from the dashboard or
  the API are written to `editor.jobs` as `queued` and run by `editor
  worker`, no longer inside the dashboard process: a restart of either
  loses nothing. Every hour the worker queues one tick per tenant instead
  of running all tenants one after the other. A tenant runs one job at a
  time, the tenant served least recently goes first and, within a tenant,
  work a person asked for comes before the hourly tick, so one tenant's
  queue cannot hold up the others. A job whose worker dies (no heartbeat
  for five minutes) is queued again, up to three attempts; an error raised
  by the work itself is recorded and not retried, so a failed draft is not
  paid for twice. `editor worker --concurrency N` (or
  `EDITOR_WORKER_CONCURRENCY`, default 2) runs up to N tenants at the same
  time on PostgreSQL; on SQLite it runs one job at a time. On SIGTERM the
  worker stops claiming and waits for the running jobs.
- The queue is the same on PostgreSQL and SQLite: Editor Mode no longer
  depends on procrastinate. `editor tick` queues every tenant's tick and
  runs the waiting work in the foreground.

### Upgrading from 0.4.0

The Core applies its migration 0004 by itself at start. For a server
installed with `deploy/server`: add `delegate: true` and a
`delegate_limits` line to the `editor-mode` token of `core.yaml` (see
`core.example.yaml`; `compose.yaml` already sets `EDITOR_CORE_DELEGATE`),
then run `editor tenant-plan <tenant> --max-cost-month <budget>` for each
tenant to give it its limit in the Core. Usage counted before under the
shared `editor` tenant stays there; each tenant starts counting as
`editor/<tenant>`.

Run `editor migrate` (PostgreSQL revisions 0011 and 0012, SQLite files
`0002_job_queue` and `0003_secrets`), then restart the dashboard and the worker. The
`procrastinate_*` tables left by earlier versions are no longer used and
can be dropped. The dashboard now only queues work: keep `editor worker`
running next to it (the local installation and `deploy/server` already
do).

For credentials from the dashboard on a server, add `EDITOR_SECRET_KEY`
to `.env` (`openssl rand -base64 32`) and keep a copy with the backups.
Without it everything works as before, with credentials in the
environment.

## 0.4.0

Two installations, both with a web interface. On your computer: one
command, data in SQLite, nothing else to install. On a server for several
tenants: PostgreSQL, the Core and Editor Mode with `docker compose`.

### Added

- **Local installation** (`install/local.sh`, Linux and macOS): one command
  puts the Core and Editor Mode in `~/.eullm-agent`, with the Python Editor
  Mode needs brought by uv, and adds the `eullm-local` command. From a
  checkout: `sh install/local.sh --source .`.
- **`eullm-agent start`**: the agent on one computer, no setup by hand. It
  opens the browser with a link that logs in by itself (a new token at
  every start, never written to disk), keeps its state in SQLite next to
  the config, uses an EuLLM Engine it finds at `localhost:11434` the first
  time (or asks for a provider) and listens on this computer only. When
  Editor Mode is installed next to the config, it also runs its dashboard
  (`127.0.0.1:8090`) and hourly worker, and prints the dashboard's login
  token; logs go to `logs/`.
- **Web interface** of the Core, served by `start` and `api` at `/`: chat
  (each message is a task), tasks with every model and tool call,
  approvals, usage of today and this month. It reads everything through
  `/v1` with the browser's token, under a strict content security policy.
- **SQLite store** for the Core (`database.url: sqlite://eullm-agent.db`):
  the same tables, append-only audit and startup recovery as PostgreSQL,
  checked by the same tests.
- **Editor Mode on SQLite** (`EDITOR_DATABASE_URL=sqlite:///editor.db`): one
  file with one tenant (a second is refused), the same code as PostgreSQL,
  and `editor worker` as a plain hourly loop. Every database test runs on
  both.
- **Server installation** (`deploy/server`): `docker compose` with
  PostgreSQL 18, the Core, Editor Mode's dashboard and worker, database
  roles created at the first start, and a guide (HTTPS, backups, updates).
  Images: `Dockerfile` (Core) and `editor-mode/Dockerfile`.
- `GET /v1/profiles`: the profiles a token may use.

### Changed

- `editor migrate` on PostgreSQL also creates the worker's job queue;
  `editor worker --apply-schema` is no longer needed.
- Editor Mode counts tenant usage in its own code instead of the
  `editor.tenant_usage` view, with days and months in the tenant's time
  zone as before.
- `Cargo.lock` is in the repository and CI and release builds use
  `--locked`, so every build of a tag uses the same dependency versions.
- Dependabot proposes dependency updates (Rust, Editor Mode, GitHub
  Actions) once a week as pull requests; minor and patch updates are
  grouped, a major version comes on its own. First round merged:
  `serde_json`, `actions/checkout` 7, `actions/upload-artifact` 7,
  `actions/download-artifact` 8, `softprops/action-gh-release` 3.
- Editor Mode: `pyyaml`, never imported, is no longer a dependency.

### Upgrading from 0.3.x

Nothing to change: PostgreSQL installations keep working as they are and
the Core applies no new migration. `editor migrate` needs no new revision
either; run it once to create the job queue if the worker used
`--apply-schema`.

## 0.3.6

### Security

- `run_program` and module tools run in a bubblewrap sandbox by default
  (`tools.sandbox`): no network, system directories read-only, only the
  workspace visible and read-only, own PID namespace (nothing a program
  starts outlives it), memory, file size and core limits. If the sandbox
  cannot work the service does not start; `tools.sandbox.mode: none` turns
  it off knowingly.
- A tainted run that has also read local files or documents needs approval
  for `fetch_url` (`taint.private_sources`, `taint.egress`), so injected
  instructions cannot send private data out.
- Editor Mode: a tenant names only its publishing secret; the server reads
  `EDITOR_SECRET_<TENANT>__<NAME>`, never another variable. Tenant ids are
  lowercase words joined by single dashes.
- Editor Mode: gzip sitemaps are inflated within the size limit (no gzip
  bomb), and a truncated one no longer stops the analysis.
- Editor Mode: robots.txt is checked under the name the Core sends
  (`eullm-agent`), also before reading the articles cited in drafts.
- Editor Mode: a deactivated tenant loses access to API and dashboard at
  once (migration 0010), not only its scheduled work.
- Editor Mode: publications connect to the address that was checked, so a
  name that changes between lookups (DNS rebinding) cannot reach internal
  addresses; environment proxies are not used for publications.
- Editor Mode: SMTP STARTTLS verifies the server certificate before the
  login sends the password.
- Monthly budgets count model calls under way at the month's average per
  call, so concurrent requests cannot all slip under the limit.
- A provider that does not report token usage no longer makes calls free:
  the Core estimates it. The `default` model can be priced (`pricing` at
  the top level), and the Core warns at start when a cost budget applies to
  a model without a price.

### Changed

- Editor Mode: source discovery and relevance checks follow the approved
  profile only (approving a version starts discovery), and maintenance
  never reverses a status a person set.

### Reliability

- Approvals: a waiting run also reads its decision from the database, so a
  decision taken through another Core process reaches it, and a decision
  that lands just before the expiry wins. At startup, runs left unfinished
  by the previous process fail as interrupted. The API stops gracefully.
- Tenant limits: concurrent runs and fetches cannot all pass the same
  check; the monthly budget is re-checked before every model call of a run.
- `api.fetch.credentials`: the Core adds a header (e.g. a GitHub token) for
  an exact host, over https and on the first request only.
- Editor Mode: background work is recorded as jobs with its outcome
  (shown on the site page and at `GET /api/jobs`). Each scheduled step
  runs on its own; maintenance and the weekly review run based on their
  last successful run, so a late tick no longer skips them. An undelivered
  briefing is sent again within its window; recipients get it in Bcc.
- Editor Mode: a publication is marked `sending` before anything leaves; one
  cut short waits for a person to check the CMS instead of being sent twice.
- A fallback answer is recorded and billed as the model that served it,
  with that model's price, not the primary's.
- An approval wait ends when its run's time limit does, so a forgotten
  approval no longer holds a run slot past the run's deadline.
- Two Core processes on the same database (a rolling restart, a second
  instance) no longer close each other's runs at startup: each process
  holds an instance lock and recovery skips runs of a live one
  (migration 0003, applied at start).
- Editor Mode: a profile edit (`PUT /api/sites/{id}/profile` and the
  dashboard) is validated and refused with 400 if malformed, instead of
  breaking the site pages.
- Editor Mode: quota checks and profile and draft version numbers are safe
  under concurrent requests.
- YAML is read and written with `serde-saphyr`; `serde_yaml` is deprecated.

## 0.3.5

### Added

- Editor Mode (`editor-mode/`, Python): the first vertical built on the
  Core, now part of this repository under AGPL-3.0-or-later. Domain-first
  editorial work: site analysis and an editorial profile with evidence,
  source discovery and rating, Hype and Opportunity scores, an editorial
  plan, drafts where every sentence cites a stored source, publication to
  WordPress, webhooks and Telegram after an owner's approval, and a
  seven-page dashboard. Plan limits per tenant on top of the Core's.

### Changed

- README: Editor Mode shown with its dashboard.
- Pull request template: no CLA section; the CLA check tells external
  contributors what to add.

## 0.3.0

The Core as a service: other programs can start runs, make model calls,
decide approvals and fetch web pages over HTTP, with state and audit in
PostgreSQL and limits per tenant. Applications such as Editor Mode are built
on this API instead of calling providers or the web directly.

### Added

- `eullm-agent api`: HTTP API (`/v1/runs`, `/v1/llm/chat`, `/v1/approvals`,
  `/v1/health`) with bearer tokens per tenant, documented in
  `docs/openapi.json` and checked by contract tests.
- `eullm-agent token new` to create API tokens (only the SHA-256 is stored).
- PostgreSQL store (`database`): schema `core` with runs, model calls, tool
  calls, approvals and an append-only `audit_events` table; migrations run
  at start. Without a database the state is kept in memory.
- Model Router (`models`): named models with fallback chains and pricing.
- Profiles (`profiles`): model, allowed tools, iteration and time limits,
  token and cost budgets per run.
- Policy engine (`policy_file`): allow / deny / require_approval per tool and
  profile; runs that read external content are tainted and their side
  effects need approval.
- Approval queue: runs wait for a decision from the API, from Telegram
  (`/approve`, `/deny`) or from the terminal; no answer in time is a refusal.
- Telegram tasks run in the background so approvals can be answered, and use
  `telegram.profile`.
- `POST /v1/fetch` (`api.fetch`): HTTP GET for applications built on the Core
  (crawling, feeds) with the same address checks as `fetch_url`, every
  redirect re-checked, a size limit and a per-host pace shared by all
  callers; each request is recorded in `core.fetches` and the audit.
- Limits per tenant (`api.tenants`): monthly cost and tokens, runs and
  fetches per day, in UTC calendar periods; a request over a limit gets 429
  with the reason. `GET /v1/usage` reports the tenant's usage and limits.

### Changed

- The audit log records the policy decision of every tool call and the
  token totals, cost and taint of every run.

## 0.2.0

Security hardening release (Sprint F0). Configurations from 0.1.x still load,
but anything risky they enabled is now off until turned on explicitly.

### Breaking changes

- `provider.type: eullm` now uses the EuLLM Engine's OpenAI-compatible
  `{base_url}/v1/chat/completions`, where tool calling works. For a plain
  Ollama server use the new `provider.type: ollama`.
- The `shell` tool is replaced by `run_program` (`tools.exec`): disabled by
  default, runs only programs in `allowed_programs`, never through a shell.
  An old `tools.shell` section enables nothing.
- File tools are confined to `workspace`; `tools.filesystem.allowed_paths` is
  ignored (a warning is printed). `write_file` needs `allow_write: true`.
- `fetch_url` is disabled by default; when enabled it allows HTTPS GET to
  public addresses only.
- Modules are disabled by default and installed only by an operator with
  `eullm-agent module install <name>`. The `install_module` tool is removed.
- `eullm-agent serve` refuses to start when `telegram.allowed_users` is empty.

### Security fixes

- Shell command execution with a bypassable `sudo` filter (S1).
- Path traversal and symlink escapes in `write_file` and `read_file`, and
  unrestricted `list_dir` (S2, S3).
- SSRF in `fetch_url`, including through redirects and DNS rebinding (S4).
- Command injection through module tool arguments, and module installation
  (with `sudo`) triggered by the model (S5).
- Telegram bot open to anyone by default (S6).
- Timed-out commands left running; unbounded tool output (S10).

### Added

- Per-request provider timeouts and one retry on 429/502/503/504 with
  `Retry-After`; error messages carry the provider's status and body.
- Run limits: `limits.max_run_seconds`, `limits.max_tool_output_bytes`.
- JSONL audit log (`audit_log`) of runs, model calls and tool calls, without
  argument values or outputs.
- Secrets from environment variables: `api_key_env`, `token_env`.
- `max_tokens` for Anthropic; token usage parsed for every provider.
- Anthropic responses with unknown block types (e.g. thinking) are accepted.
- Malformed tool-call arguments are reported to the model instead of running
  the tool with defaults.
- `eullm-agent module list|install` subcommands.
- Security, provider and agent regression tests (`tests/`).
