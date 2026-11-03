# AI Platform — architektura a dohodnuté principy

**Stav návrhu:** 3. listopadu 2026  
**Projekt:** `martelnov/ai-platform`  
**Účel dokumentu:** zaznamenat dohodnuté cílové chování. **Neznamená to, že je vše již implementováno.** Aktuální stav kódu a ověřených funkcí je v [PROJECT_STATE.md](PROJECT_STATE.md).

## 1. Cíl

Vytvořit přenosnou víceuživatelskou platformu pro sdílení jedné nebo více GPU. Uživatel má vlastní trvalou síťovou identitu a úložiště; aplikace (ComfyUI, Ollama/Open WebUI, ACE-Step a další) se spouštějí pouze podle potřeby. Rezervace GPU se odděluje od přístupu do uživatelského prostředí.

Platformu provozuje **jeden správce**, který má práva `root` na hostiteli a je administrátorem aplikace. Ostatní uživatelé nedostávají administrátorská oprávnění hostitele.

Hlavní zásady:

1. **Tailscale je vstupní síťová vrstva.** Služby nevystavovat do veřejného internetu.
2. **Jeden persistentní `ai-ts-<id>` na uživatele.** Jeho Tailscale identita a DNS zůstávají stabilní i mezi rezervacemi.
3. **Rozcestník je dostupný stále; pracovní aplikace jsou on-demand.** API požadavek aplikaci automaticky nespustí.
4. **O GPU rozhoduje centrální plánovač, ne aplikace ani uživatelský kontejner.** Bez rezervace aplikace GPU vůbec nedostane.
5. **Data a konfigurace jsou mimo dočasné aplikační kontejnery.** Restart/recreate aplikace nesmí smazat uživatelskou práci.
6. **Privilegované operace provádí host-agent na hostiteli.** Uživatelské služby nemají Docker socket ani root přístup na hostitele.
7. **Projekt má být přenositelný na jiný server.** Hardware, limity a cesty nesmějí být napevno zakódovány do uživatelské logiky.

## 2. Zamýšlená topologie

```text
Správce přes Tailscale
        |
        +--> Tailscale Serve hostitele --> Control Plane (Python / FastAPI)
                                             |       |
                                             |       +--> PostgreSQL
                                             +--> Unix socket --> Host-agent (root)
                                                                    |
Uživatel s nasdíleným Tailscale strojem                         správa Dockeru,
        |                                                        mountů, fscrypt
        v
  ai-ts-<id>  [TRVALE]
    - Tailscale node / identita / HTTPS
    - velmi lehký rozcestník (cílový návrh)
        |
        +--> Gateway API (Python, centrální) --> Host-agent
        |
        +--> File manager          [ON DEMAND, bez GPU rezervace]
        +--> ComfyUI               [ON DEMAND, 0/1/N GPU]
        +--> Ollama / Open WebUI   [ON DEMAND, 0/1/N GPU]
        +--> ACE-Step              [ON DEMAND, 0/1/N GPU]
        +--> další služby          [ON DEMAND]
```

Na úrovni uživatelského přístupu je žádoucí sdílený síťový namespace aplikací s příslušným `ai-ts-<id>` (např. Docker `--network container:ai-ts-<id>`), aby jednotlivé služby nepotřebovaly veřejné porty na hostiteli. **Je to cílová topologie; současný host-agent zatím nemá kompletní orchestrace všech aplikací.**

Tailscale konektor **nesmí spouštět Docker přímo**. Pouze obsluhuje webové požadavky a volá důvěryhodné centrální API. Host-agent vykoná povolenou operaci.

## 3. Uživatelský konektor a rozcestník

Při zřízení uživatele platforma vytvoří jeho Linux identitu, pracovní adresáře a trvalý Tailscale konektor `ai-ts-<id>`. Uživatel dostane jednou přístup k jeho zařízení prostřednictvím Tailscale machine sharing. Konkrétní automatizace pozvání se ještě musí ověřit; není předpokládána jako hotová.

`ai-ts-<id>` běží i bez rezervace GPU. Má být **co nejmenší na RAM i CPU**. Rozcestník nabídne alespoň:

| Funkce | Požadavek na GPU | Cílový provoz |
| --- | --- | --- |
| Přehled dostupných služeb a jejich stavu | ne | stále dostupný |
| Správa šifrování / odemykání úložišť | ne | lehké UI + centrální API |
| Rezervační kalendář | ne | lehké UI + centrální API |
| Správce souborů | ne | samostatná služba on-demand |
| Správa modelů / LoRA | ne | podle možností aplikace, bez rezervace |
| ComfyUI | ne při přípravě; ano pro GPU výpočet | jeden aplikační kontejner na instanci |
| Ollama / Open WebUI | ne pro CPU režim; ano pro GPU | jeden aplikační kontejner na instanci |
| ACE-Step | podle režimu práce | jeden aplikační kontejner na instanci |

**Technologie rozcestníku zatím není finálně vybrána.** Kandidátem je statické HTML poskytované pomocí Tailscale Serve a malý bezpečný backend pro akce; případně velmi malý Python HTTP proces. Samotné statické HTML nestačí pro bezpečné ovládání privilegovaných funkcí. Existující `gateway/main.go` je starý prototyp, nikoli žádoucí směr nového vývoje. Vlastní aplikační kód projektu chceme držet v Pythonu; externí AI produkty se nepřepisují.

## 4. Životní cyklus aplikací a jejich API

1. Uživatel otevře svůj **stále dostupný** rozcestník.
2. V případě potřeby nejdříve odemkne odpovídající šifrované prostory.
3. Ručně zvolí **Spustit** u konkrétní služby.
4. Platforma ověří oprávnění, požadovaná úložiště a případnou rezervaci GPU a spustí aplikaci s odpovídajícími zdroji.
5. Dokud aplikace běží, funguje její webové rozhraní **i původní API**. To platí zejména pro Ollamu používanou z VS Code, Visual Studia, Pythonu nebo jiných klientů.
6. Po ručním zastavení je služba nedostupná. **Příchozí API volání ji nesmí samo probudit.**

Příklad toku pro Ollamu:

```text
otevřít https://<uživatelův-tailscale-node>/
    -> případně odemknout modely/data
    -> ručně spustit Ollamu
    -> externí klient zavolá Ollama /api/chat nebo /v1/chat/completions
    -> po zastavení aplikace už API neodpovídá
```

Stabilní URL a porty se teprve stanoví. Proxy musí zachovat streamování (včetně dlouhotrvajících odpovědí) a pro aplikace typu ComfyUI také WebSockety. U vypnuté služby stačí chybová odpověď / nedostupný backend; **bez auto-startu**.

Aplikace po spuštění běží **v jednom aplikačním kontejneru**. Přechod mezi CPU-only a GPU režimem může vyžadovat její zastavení a opětovné vytvoření: Docker standardně neumí dodatečně přidělit GPU již vytvořenému kontejneru pouhou změnou CPU limitů. Pracovní data musí proto být persistentní. Zachování neuloženého stavu otevřeného editoru při přepnutí je dosud **otevřený implementační úkol**, ne garantovaná vlastnost.

Automatické ukončování nečinných služeb je možnost do budoucna, **nikoli dohodnuté hotové pravidlo**. Nikdy nesmí ukončit probíhající výpočet nebo aktivní API požadavek.

## 5. GPU a CPU plánování

### GPU

- Rezervace požaduje **počet GPU**, nikoli napevno aplikaci: po získání rezervace může uživatel přecházet mezi různými podporovanými aplikacemi.
- 0 GPU = přípravný režim, žádný přístup k GPU zařízením.
- 1 GPU = výhradně přidělená konkrétní karta.
- 2 GPU = obě karty mohou být přiděleny **jednomu aplikačnímu kontejneru jednoho uživatele** (např. Ollama, některé workflow ComfyUI).
- Obecně **N GPU**: datový model má zůstat rozšiřitelný. Přidělení více karet musí být atomické (všechny požadované, nebo žádná).
- Dvě RTX 3060 12 GB **neznamenají jednu univerzální 24GB VRAM**. Využití obou závisí na aplikaci/modelu.
- Jednotlivé GPU identifikovat stabilně pomocí GPU UUID, nikoli jen pořadového čísla v Dockeru.

### CPU bez rezervace

Všechny **výpočetní AI kontejnery bez GPU rezervace** mají dohromady sdílet velmi malý CPU fond, původně uvažovaný ekvivalent **jednoho logického CPU celkem**. **Ne jedno CPU na uživatele ani na kontejner.** Velikost fondu nastaví administrátor podle finálního hardware. Lze realizovat společnou nadřazenou cgroup s agregovanou CPU kvótou; konkrétní způsob je nutné implementovat a změřit.

Služby potřebné pro ovládání platformy a souborový přístup nesmějí být tímto fondem zablokovány. Pro rezervovanou GPU relaci se nastaví vyšší CPU/RAM limity podle profilu služby. Limity nejsou napevno dané procesorem v první sestavě.

### Datový model: co již existuje

Migrace `001_base.sql` obsahuje `gpu_devices` (včetně `gpu_uuid`) a `application_profiles` (`gpu_min`, `gpu_max`, `cpu_limit`, `memory_limit_bytes`). Migrace `002_runtime.sql` obsahuje `reservations.gpu_count`, `workloads` s režimy `preparation` a `gpu` a vazební tabulku `workload_gpus`. **Existence tabulek ještě neznamená hotový a otestovaný plánovač.** Při implementaci je nutné teprve prověřit, zda je třeba doplnit vazbu konkrétní rezervace na konkrétní GPU; nevytvářet druhou duplicitní tabulku bez revize stávajícího návrhu.

## 6. Úložiště, uživatelé, kvóty a šifrování

### Uživatelské prostory

- Základní stromy jsou **DATA** a **MODELS** (`/AI/data/users/<username>`, `/AI/models/users/<username>`).
- Každý uživatel má vlastní Linux UID/GID a vlastní přístupová práva; podrobnosti přiřazení řídí hostitelská platforma.
- Modely mohou být ve společném nezakódovaném/nešifrovaném prostoru pro sdílení a deduplikaci; soukromé uživatelské modely mohou být podle přiděleného backing filesystemu šifrované.
- Diskové kvóty jsou **nativní filesystem quotas**; hodnoty a požadovaný stav jsou v PostgreSQL. Neimplementovat paralelní softwarovou simulaci kvót.

### Mounty

- Administrátor smí uživateli přidělit i adresář / blokové zařízení z jiného místa hostitele (případně sítě), protože sám spravuje celý server.
- Běžný uživatel nesmí libovolně mountovat lokální systémové cesty. Možnost připojovat jeho vlastní síťové zdroje (SMB/NFS) je samostatné plánované oprávnění s validací a limity.
- Uživatel vidí přidělené mounty. Vlastní mount/unmount operace provádí privilegovaný host-agent, **ne** dlouhodobě běžící Tailscale konektor ani krátkodobý privilegovaný storage kontejner.
- Mounty a přístup k jejich datům musejí být připraveny **před** spuštěním cílové aplikace. Zastavení aplikace nesmí samo smazat uložená data.

### fscrypt

- **Administrátor definuje, které konkrétní podadresáře smějí být šifrovatelné; uživatel si je sám inicializuje, odemyká, zamyká a mění heslo.**
- fscrypt je vlastnost **reálného backing filesystemu**, ne logického názvu stromu DATA/MODELS.
- Na základním `/AI/models` se fscrypt nepovoluje kvůli plánovanému sdílení/deduplikaci modelů; přesto **nesmíme zakázat fscrypt v celé větvi MODELS**. Šifrovaný root může ležet uvnitř připojeného jiného filesystemu, který fscrypt skutečně podporuje a je připravený.
- Základní `010_user_encryption_roots.sql` blokuje překryvy mountů oběma směry. Nová `011_allow_encryption_under_mount.sql` má povolit **encryption root uvnitř již existujícího mountu** (např. mount `/private`, encryption `/private/secret`), ale dál odmítat stejnou cestu, mount uvnitř encryption rootu a dodatečný překrývající mount. Migrace `010` se zpětně nepřepisuje.
- Před inicializací šifrování musí host-agent ověřit skutečný filesystem a jeho podporu/setup fscrypt. **To je nutné ještě prakticky otestovat i na podadresáři připojeného filesystemu.**
- Soubory odemčené na serveru jsou dostupné hostitelskému `root`. Toto řešení **nechrání plaintext před administrátorem v době, kdy je úložiště odemčené**.

### VeraCrypt — zatím pouze úvaha

Vedle fscrypt se diskutovala podpora uživatelem vytvořeného přenositelného `.vc` vaultu: soubor se na server nahraje (nebo přenese fyzicky), uživatel jej odemkne, použije a zase zamkne a může si jej stáhnout. Možná je též pozdější synchronizace rozdílů mezi identickými kopiemi. **VeraCrypt zatím není součástí hotové implementace ani aktuální priority.**

## 7. Bezpečnostní hranice

- `root` na hostiteli a administrace platformy patří jedné důvěryhodné osobě.
- Tailscale omezuje síťový dosah, ale **nenahrazuje vnitřní autorizaci**. Běžný uživatel nesmí ovládat jiného uživatele ani číst jeho data.
- `gateway-api` musí ověřit, že volající konektor je skutečně důvěryhodný. Samotné uživatelsky podvrhnutelné HTTP hlavičky nejsou dostačující tam, kde existuje přímá interní cesta k API. Konkrétní mechanismus dosud čeká na návrh/ověření.
- Administrativní identifikace z Tailscale Serve hlaviček je platná jen při vynucené důvěryhodné cestě přes proxy.
- Uživatelská služba nesmí dostat Docker socket, přístup k PostgreSQL ani socket host-agenta; žádné privilegované kontejnery pro běžnou práci.
- Veřejný GitHub je přípustný pro **zdrojový kód**, nikdy pro produkční hesla, klíče, autentizační tokeny či jiné secrets. Změna viditelnosti repozitáře nezaručuje čistou historii commitů.

## 8. Hardware a přenositelnost

Uvažovaná sestava pro GPU server je **Dell Precision T3610**, jeden **Xeon E5-2697 v2** (12 jader / 24 vláken), nejvýše **2× NVIDIA RTX 3060 12 GB**. Druhá karta by případně měla vlastní externí ATX zdroj. Jde o **plánovanou možnost**, nikoli doklad, že jsou obě GPU již osazené nebo že na ní nyní běží `ai-platform`.

Současné vývojové/testovací výpisy pocházejí ze serveru `ailab` s Ubuntu 24.04.5 LTS. Hardwarové parametry a skutečně instalované GPU je nutné vždy ověřit přímo na hostiteli. Software musí fungovat i na budoucím stroji s jiným počtem GPU.

## 9. Co ještě výslovně není rozhodnuto

- Přesná realizace a rozměr trvale běžícího webového rozcestníku (statika + backend vs. minimální Python server).
- Definitivní URL/portová mapa jednotlivých API; musí zachovat Ollama API bez automatického spuštění.
- Bezpečný mechanismus propojení per-user konektoru, Gateway API a host-agenta a způsob prokázání identity.
- Implementace multi-GPU alokace, sdílené CPU cgroup, podmínky přechodu CPU ↔ GPU a ukládání rozpracovaného UI stavu.
- Konkrétní produkt správce souborů a správce modelů; podporované aplikace se mají přidávat bez přepisování jádra platformy.

## 10. Pravidlo pro další vývoj

Před každou větší změnou porovnat **aktuální kód na GitHubu** s **aktuálním stavem běžícího serveru**. Nikdy nepokládat plánované chování za již hotové jen proto, že existuje databázová tabulka nebo ukázkové HTML. Po ověření funkce doplnit [PROJECT_STATE.md](PROJECT_STATE.md) a zdokumentovat rozhodnutí, které změnilo architekturu.
