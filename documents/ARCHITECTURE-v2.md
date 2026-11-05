# AI Platform — architektura a dohodnuté principy

**Stav návrhu:** 5. listopadu 2026  
**Projekt:** `martelnov/ai-platform`  
**Návaznost na kód:** veřejný `main`, kontrolovaný do commitu `a2a09ae`  
**Účel dokumentu:** zaznamenat dohodnuté cílové chování a technickou topologii. **Neznamená to, že je vše již implementováno, nasazeno nebo otestováno.** Aktuální stav je v [PROJECT_STATE.md](PROJECT_STATE.md).

## 1. Cíl

Vytvořit přenosnou víceuživatelskou platformu pro sdílení jedné nebo více GPU. Každý uživatel má vlastní trvalou síťovou identitu a úložiště. Aplikace (ComfyUI, Ollama/Open WebUI, ACE-Step a další) se spouštějí pouze podle potřeby. **Rezervace GPU je oddělena od přístupu k uživatelskému prostředí.**

Platformu provozuje **jeden správce**, který má práva `root` na hostiteli a je administrátorem aplikace. Ostatní uživatelé nedostávají administrátorská oprávnění hostitele.

Hlavní zásady:

1. **Tailscale je vstupní síťová vrstva.** Uživatelské služby nevystavovat do veřejného internetu.
2. **Jeden persistentní `ai-ts-<id>` na uživatele.** Jeho Tailscale identita a DNS mají zůstat stabilní i mezi rezervacemi.
3. **Lehký Python rozcestník je dostupný stále; pracovní aplikace jsou on-demand.** Příchozí API požadavek vypnutou aplikaci automaticky nespustí.
4. **O GPU rozhoduje centrální plánovač**, ne aplikace ani uživatelský kontejner. Bez rezervace aplikace GPU nedostane.
5. **Data a konfigurace jsou mimo dočasné aplikační kontejnery.** Recreate aplikace nesmí smazat uloženou práci.
6. **Privilegované operace vykonává host-agent na hostiteli.** Uživatelské služby nemají Docker socket ani root přístup na hostitele.
7. **Platforma má být přenositelná na jiný server.** Hardware, limity a cesty nesmějí být napevno zadrátovány do uživatelské logiky.
8. **Autentizace i autorizace musí být vynucena uvnitř platformy.** Samotná přítomnost zařízení v Tailscale není oprávněním ovládat cizí data či kontejnery.

## 2. Topologie platformy

```text
Správce přes Tailscale
         |
         +--> Tailscale Serve hostitele --> Control Plane (Python / FastAPI)
                                               |       |
                                               |       +--> PostgreSQL
                                               +--> Unix socket --> Host-agent (root)
                                                                      |
Uživatel s nasdíleným Tailscale strojem                        Docker, mounty,
         |                                                     fscrypt, provisioning
         v
   ai-ts-<id>                              [TRVALE: Tailscale identita]
     |
     | Tailscale Serve :443 -> 127.0.0.1:8080
     v
   ai-launcher-<id>                        [TRVALE: Python rozcestník]
     |   (sdílený network namespace s ai-ts-<id>)
     |
     +--> GET /        HTML uživatelský rozcestník
     +--> GET /health  stav launcheru
     +--> GET /api/me  -> Gateway API (Python, centrální)
                               |
                               +--> PostgreSQL
                               +--> Unix socket --> Host-agent

   Pracovní aplikace uživatele:           [ON DEMAND]
     +--> Správce souborů                  (bez GPU rezervace)
     +--> ComfyUI                          (0/1/N GPU podle režimu)
     +--> Ollama / Open WebUI              (0/1/N GPU podle režimu)
     +--> ACE-Step                         (podle režimu)
     +--> další služby
```

Control Plane a Gateway API jsou centrální služby. Tailscale konektor slouží jako trvalý síťový vstup; **HTTP aplikace rozcestníku běží v samostatném Python launcheru**. Launcher nespravuje Docker přímo a privilegované akce musí jít přes důvěryhodné API a host-agenta.

U pracovních aplikací je cílem přístup přes příslušný uživatelský Tailscale node bez veřejných hostitelských portů. Možností je sdílený síťový namespace s `ai-ts-<id>` (např. `--network container:ai-ts-<id>`), ale je nutné řešit konflikty portů, izolaci služeb a budoucí routing. **Kompletní runtime a proxy pracovních aplikací zatím nejsou implementovány.**

## 3. Uživatelský konektor a rozcestník

### 3.1 Životní cyklus konektoru

Při zřízení uživatele platforma vytvoří Linux identitu, pracovní adresáře a trvalý Tailscale konektor `ai-ts-<id>`. Uživatel dostane přístup prostřednictvím Tailscale machine sharing. Konkrétní automatizace pozvání se ještě musí ověřit; není považována za hotovou.

`ai-ts-<id>` běží i bez GPU rezervace. Má mít nízkou spotřebu RAM a CPU. Nezískává Docker socket ani právo spouštět libovolné kontejnery. Jeho úkolem je síťová identita a HTTPS vstup.

### 3.2 Rozhodnutá technologie rozcestníku

**Rozhodnutí k 5. 11. 2026:** rozcestník používá **malý Python HTTP server** založený na standardní knihovně (`http.server`), nikoli Go a nikoli těžký aplikační framework. HTML tvoří `gateway/index.html`; server je v `gateway/launcher.py`. Obraz se sestavuje pomocí `gateway/Dockerfile.python` z `python:3.12-alpine`.

Pro každého uživatele existují dva oddělené, trvale zamýšlené kontejnery:

- `ai-ts-<id>` — persistentní Tailscale konektor;
- `ai-launcher-<id>` — neprivilegovaný Python HTTP server.

Launcher používá:

```text
--network container:ai-ts-<id>
AI_CONNECTOR_PROVIDER_ID=<provider-id daného Tailscale node>
AI_GATEWAY_API_URL=http://gateway-api:8080
```

Server naslouchá na `127.0.0.1:8080` ve společném síťovém namespace. **Nevystavuje vlastní port na hostiteli.** Tailscale Serve v konektoru má přesměrovat HTTPS na `http://127.0.0.1:8080`.

| Endpoint | Funkce | Stav v kódu |
| --- | --- | --- |
| `GET /` | HTML rozcestník | implementováno |
| `GET /health` | jednoduchý health check | implementováno |
| `GET /api/me` | identita aktuálního konektoru/uživatele přes Gateway API | základ implementován; bezpečnostní hranici nutno posílit |

Rozcestník nyní zobrazuje jméno uživatele, název konektoru a informační karty služeb. **Ovládání start/stop, šifrování a rezervací v něm ještě nefunguje.**

Historické `gateway/main.go` a `gateway/Dockerfile` patří ke Go prototypu. Nejsou cílovým řešením, ale lze je odstranit teprve po ověření, že nejsou součástí skutečného provozu.

### 3.3 Identita a důvěryhodná cesta

`ai-platform-launcher-manager.py` získá provider ID z `tailscale status --json` (`Self.ID`) příslušného konektoru a vloží je při vytvoření launcheru do proměnné `AI_CONNECTOR_PROVIDER_ID`.

Při `GET /api/me` launcher serverově zavolá centrální Gateway API na `/v1/me` a pošle hlavičku:

```http
X-AI-Connector-ID: <provider-id>
```

Browser nedodává identifikátor konektoru jako libovolný vstup. Centrální Gateway API podle identifikátoru vyhledá odpovídajícího uživatele v PostgreSQL.

**Tento základ ale ještě není bezpečnou autentizací volajícího.** Interní síťový klient schopný zavolat Gateway API by mohl deklarovat jiné `X-AI-Connector-ID`. Před zpřístupněním privilegovaných akcí je nutné vytvořit důvěryhodný mechanismus ověření skutečného původu požadavku a oddělení uživatelů. Identita Tailscale zařízení na vnější hranici sama o sobě nedokazuje, který interní kontejner volá API.

### 3.4 Bezpečnost launcheru

Launcher je navržen jako neprivilegovaný kontejner:

- UID/GID `65532:65532`;
- read-only root filesystem;
- `--cap-drop ALL`;
- `--security-opt no-new-privileges`;
- bez Docker socketu;
- bez host-agent socketu;
- bez přímého přístupu k PostgreSQL.

Sám neprovádí privilegované operace, mounty ani správu Dockeru. Všechny budoucí uživatelské akce musí být autorizované v centrální části platformy a vykonané host-agentem, pokud vyžadují hostitelská oprávnění.

### 3.5 Automatická správa launcherů

Správu provádí hostitelský program `host/ai-platform-launcher-manager.py`, který podporuje zajištění konkrétního konektoru i `--all`.

Pro `ai-ts-<id>` manager:

1. Ověří existenci, běh a Tailscale stav `Running`.
2. Získá identitu Tailscale node.
3. V případě potřeby připojí Tailscale kontejner do Docker sítě `ai-gateway` kvůli centrálnímu Gateway API.
4. Ověří existující `ai-launcher-<id>` (běh, namespace, identitu a URL API).
5. Chybějící nebo neodpovídající launcher vytvoří/obnoví z `ai-platform-launcher:latest`.
6. Ověří Tailscale Serve a při prázdné konfiguraci jej nastaví pro HTTPS proxy na launcher.

Pokud je konfigurace Serve již neprázdná, ale neodpovídá očekávané podobě, manager ji **nepřepíše naslepo**, nýbrž oznámí chybu.

Host-agent spouští manager po úspěšném provisioningu Tailscale konektoru. Pokud launcher selže, vytvořený konektor se neruší; vrací se `launcher_warning`.

Pravidelné kontroly mají zajišťovat:

- `install/systemd/ai-platform-launchers.service` — jednorázové spuštění `--all`;
- `install/systemd/ai-platform-launchers.timer` — `OnBootSec=1min`, `OnUnitInactiveSec=2min`, `AccuracySec=15s`.

Kontejner má restart policy `no`; jeho obnovu zajišťuje tento reconciliation mechanismus. **Manager kontroluje jen běžící `ai-ts-<id>` a neobnovuje zastavené Tailscale konektory.**

Důležité provozní omezení: při existujícím zdravém launcheru manager **neporovnává digest image**. Nasazení nové verze `ai-platform-launcher:latest` proto samo o sobě nezpůsobí recreate již běžících launcherů. Mechanismus řízených aktualizací je otevřený úkol.

Tyto soubory existují v Gitu. Skutečná instalace service/timeru a běh na `ailab` **nejsou aktuálními výpisy doloženy**.

### 3.6 Co rozcestník nabídne

| Funkce | Požadavek na GPU | Cílový provoz |
| --- | --- | --- |
| Přehled dostupných služeb a jejich stavu | ne | vždy dostupný Python launcher |
| Správa šifrování / odemykání úložišť | ne | lehké UI + centrální API |
| Rezervační kalendář | ne | lehké UI + centrální API |
| Správce souborů | ne | samostatná služba on-demand |
| Správa modelů / LoRA | ne | podle možností aplikace, bez rezervace |
| ComfyUI | ne při přípravě; ano pro GPU výpočet | jeden aplikační kontejner na instanci |
| Ollama / Open WebUI | ne pro CPU režim; ano pro GPU | jeden aplikační kontejner na instanci |
| ACE-Step | podle režimu práce | jeden aplikační kontejner na instanci |

**Rozcestník je součástí trvalé infrastruktury uživatele; pracovní aplikace nejsou.**

## 4. Životní cyklus aplikací a jejich API

1. Uživatel otevře svůj **stále dostupný** rozcestník.
2. Podle potřeby nejdříve odemkne odpovídající šifrované prostory.
3. Ručně zvolí **Spustit** u konkrétní služby.
4. Platforma ověří oprávnění, potřebná úložiště a případnou GPU rezervaci; poté spustí aplikaci s povolenými prostředky.
5. Dokud aplikace běží, funguje její webové rozhraní **i původní API**. To je důležité hlavně pro Ollamu používanou z VS Code, Visual Studia, Pythonu a dalších klientů.
6. Po ručním zastavení je služba nedostupná. **Příchozí API volání ji nesmí samo probudit.**

Příklad cílového toku pro Ollamu:

```text
otevřít uživatelův Tailscale HTTPS rozcestník
    -> případně odemknout modely/data
    -> ručně spustit Ollamu
    -> externí klient zavolá Ollama /api/chat nebo /v1/chat/completions
    -> po zastavení aplikace API přestane být dostupné
```

Stabilní URL a porty pracovních aplikací se teprve stanoví. Proxy musí zachovat streamování (včetně dlouhotrvajících odpovědí) a pro aplikace typu ComfyUI také WebSockety. Vypnutá služba může vracet chybu/nedostupný backend, **nikdy neprovádí auto-start**.

Aplikace po spuštění běží **v jednom aplikačním kontejneru**. Přechod mezi CPU-only a GPU režimem může vyžadovat její zastavení a opětovné vytvoření: Docker standardně neumí dodatečně přidělit GPU již vytvořenému kontejneru pouze změnou CPU limitů. Pracovní data musí být persistentní. Zachování neuloženého stavu otevřeného editoru při přepnutí je zatím **otevřený úkol**, ne garantovaná vlastnost.

Automatické ukončování nečinných služeb je možnost do budoucna, nikoli aktuální pravidlo. Nesmí ukončit probíhající výpočet nebo aktivní API požadavek.

## 5. GPU a CPU plánování

### GPU

- Rezervace požaduje **počet GPU**, nikoli napevno konkrétní aplikaci: po získání rezervace může uživatel mezi podporovanými aplikacemi přecházet.
- **0 GPU:** přípravný režim, žádný přístup k GPU zařízením.
- **1 GPU:** výhradně přidělená konkrétní karta.
- **2 GPU:** obě karty mohou být přiděleny **jednomu aplikačnímu kontejneru jednoho uživatele** (např. Ollama, některé workflow ComfyUI).
- **N GPU:** datový model zůstává rozšiřitelný; alokace více karet musí být atomická (všechny požadované, nebo žádná).
- Dvě RTX 3060 12 GB **neznamenají jednu univerzální 24GB VRAM**. Využití obou závisí na modelu a aplikaci.
- GPU identifikovat stabilně pomocí UUID, nikoli jen aktuálním pořadovým číslem v Dockeru.

### CPU bez rezervace

Všechny **výpočetní AI kontejnery bez GPU rezervace** mají dohromady sdílet velmi malý CPU fond, původně uvažovaný ekvivalent **jednoho logického CPU celkem**. **Ne jedno CPU na uživatele ani na kontejner.** Velikost fondu nastaví administrátor podle finálního hardware.

Možnou realizací je společná nadřazená cgroup s agregovanou CPU kvótou. Konkrétní řešení je třeba implementovat, změřit a otestovat. Služby pro ovládání platformy a souborový přístup nesmějí být tímto fondem blokovány. Pro GPU relaci se nastaví vyšší CPU/RAM limity podle aplikačního profilu; limity nejsou napevno svázané s procesorem prvního serveru.

### Datový model: co již existuje

Migrace `001_base.sql` obsahuje `gpu_devices` (včetně `gpu_uuid`) a `application_profiles` (`gpu_min`, `gpu_max`, `cpu_limit`, `memory_limit_bytes`). Migrace `002_runtime.sql` obsahuje `reservations.gpu_count`, `workloads` s režimy `preparation` a `gpu` a `workload_gpus`.

**Existence tabulek neznamená hotový a otestovaný plánovač.** Při implementaci je třeba prověřit, zda doplnit vazbu konkrétní rezervace na konkrétní GPU. Nevytvářet druhý duplicitní datový model bez revize stávajícího schématu.

## 6. Úložiště, uživatelé, kvóty a šifrování

### Uživatelské prostory

- Základní stromy jsou **DATA** a **MODELS** (`/AI/data/users/<username>`, `/AI/models/users/<username>`).
- Každý uživatel má vlastní Linux UID/GID a oprávnění; přidělení řídí hostitelská platforma.
- Modely mohou být ve společném nešifrovaném prostoru pro sdílení a deduplikaci. Soukromé uživatelské modely mohou být podle backing filesystemu šifrované.
- Kvóty jsou **nativní filesystem quotas**. Požadovaný stav a hodnoty jsou v PostgreSQL; nevytvářet paralelní softwarovou simulaci kvót.

### Mounty

- Administrátor může uživateli přidělit adresář nebo blokové zařízení z jiného místa hostitele, případně ze sítě, protože spravuje celý server.
- Běžný uživatel nesmí libovolně připojovat lokální systémové cesty. Možnost jeho vlastních síťových zdrojů (SMB/NFS) je samostatné plánované oprávnění s validací a limity.
- Uživatel vidí své přidělené mounty. Mount/unmount vykonává privilegovaný **host-agent**, nikoli trvalý Tailscale konektor nebo krátkodobý privilegovaný storage kontejner.
- Mounty a přístup k jejich datům musí být připraveny **před** spuštěním cílové aplikace. Zastavení aplikace nesmí smazat data.

### fscrypt

- **Administrátor definuje konkrétní podadresáře, ve kterých je šifrování povoleno; uživatel je sám inicializuje, odemyká, zamyká a mění heslo.**
- fscrypt závisí na **skutečném backing filesystemu**, nikoli jen na názvu logického stromu DATA/MODELS.
- Na základním `/AI/models` se fscrypt nepovoluje kvůli sdílení/deduplikaci modelů. To **není zákaz fscrypt v celé větvi MODELS**: šifrovaný root smí ležet na vhodném připojeném filesystemu uvnitř MODELS.
- Migrace `010_user_encryption_roots.sql` původně blokovala překryvy s mounty oběma směry. **Migrace `011_allow_encryption_under_mount.sql` je nyní v Gitu** a předefinuje `check_user_encryption_root_overlap()` asymetricky: šifrovaný root může být uvnitř existujícího mountu, nikoli na stejné cestě, nad ním ani v konfliktu s jiným šifrovaným rootem.
- Opačná kontrola zakazující dodatečně zavedený kolidující mount zůstává; migrační historii `010` neměnit.
- Před inicializací musí host-agent ověřit reálný backing filesystem a jeho podporu/setup fscrypt. **Migrační změnu i praktický fscrypt pod mountem je ještě nutné ověřit na serveru.**
- Soubory odemčené na serveru jsou dostupné hostitelskému `root`. Toto řešení **nechrání plaintext před administrátorem v době, kdy je úložiště odemčené**.

### VeraCrypt — zatím pouze úvaha

Diskutovala se podpora přenositelného `.vc` vaultu vytvořeného uživatelem: soubor nahraje nebo fyzicky přenese, odemkne, používá, zamkne a může stáhnout. Později připadá v úvahu synchronizace rozdílů mezi identickými kopiemi. **VeraCrypt není součástí hotové implementace ani aktuální priority.**

## 7. Bezpečnostní hranice

- Hostitelský `root` a administrace platformy patří jedné důvěryhodné osobě.
- Tailscale omezuje síťový dosah, ale **nenahrazuje vnitřní autorizaci**. Uživatel nesmí ovládat jiného uživatele ani číst jeho data.
- Současný `gateway-api` identifikuje konektor podle `X-AI-Connector-ID`. To je **deklarovaná identita**, nikoli samo o sobě prokázaný původ požadavku. Před privilegovanými operacemi je nezbytné doplnit důvěryhodné ověření.
- Administrativní identifikace pomocí `Tailscale-User-*` hlaviček je platná jen při vynucené důvěryhodné cestě přes Tailscale Serve.
- Launcher ani aplikační kontejnery nesmějí mít Docker socket, host-agent socket, přímý PostgreSQL přístup nebo oprávnění k datům jiných uživatelů.
- Sdílený síťový namespace s `ai-ts-<id>` je nutné posoudit z hlediska přístupu jednotlivých služeb k interním sítím a vzájemné izolace.
- Pro privilegované operace řešit autorizaci konkrétního uživatele, povolené cesty, limity i situaci, kdy hostitelská změna proběhla, ale DB zápis selhal.
- Veřejný GitHub je vhodný pro **zdrojový kód**, nikdy pro hesla, klíče, tokeny a jiné secrets. Změna viditelnosti repozitáře nevyčistí historické commity.

## 8. Hardware a přenositelnost

Uvažovaná sestava pro GPU server je **Dell Precision T3610**, jeden **Xeon E5-2697 v2** (12 jader / 24 vláken), nejvýše **2× NVIDIA RTX 3060 12 GB**. Druhá karta by případně měla externí ATX zdroj. Jde o **plánovanou možnost**, nikoli důkaz, že jsou obě GPU už instalované nebo že právě na tomto stroji běží `ai-platform`.

Současné vývojové/testovací výpisy pocházejí ze serveru `ailab` s Ubuntu 24.04.5 LTS. Skutečný hardware a dostupné GPU je nutné vždy ověřit přímo na hostiteli. Software má fungovat i na jiném serveru s jiným počtem GPU.

## 9. Co ještě výslovně není rozhodnuto

- Technologie základního rozcestníku **už rozhodnutá je: Python `http.server` v samostatném launcher kontejneru**. Otevřené jsou konečné uživatelské funkce a vzhled, důvěryhodné ověření identity a řízené aktualizace image.
- Definitivní URL/portová mapa pracovních aplikací a API proxy; musí zachovat Ollama API bez automatického spouštění.
- Bezpečný mechanismus prokazování identity per-user konektoru vůči Gateway API, včetně hranic interních Docker sítí.
- Multi-GPU alokace, společná CPU cgroup, podmínky přechodu CPU ↔ GPU a zachování neuloženého UI stavu.
- Konkrétní správce souborů a správce modelů. Podporované aplikace se mají přidávat bez přepisování jádra platformy.
- Postup automatizovaného rollout/recreate již běžících launcherů při změně image a obnova samotných zastavených Tailscale konektorů.

## 10. Pravidlo pro další vývoj

Před každou větší změnou porovnat **aktuální kód na GitHubu** s **aktuálním stavem běžícího serveru**. Nikdy nepokládat plánované chování za hotové jen proto, že existuje databázová tabulka, skript nebo HTML. Po ověření funkce doplnit [PROJECT_STATE.md](PROJECT_STATE.md); změny rozhodnutí a síťové/bezpečnostní topologie zaznamenávat zde.

**Kontrolovaný checkpoint: `a2a09ae` (5. 11. 2026). Kód Python launcheru a jeho manageru je v repozitáři; provozní nasazení a integrační testy nejsou tímto dokumentem potvrzeny.**
