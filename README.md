# MVM Next Energy Import

Home Assistant egyéni integráció, amely az [MVM Next](https://next.mvm.hu/) ügyfélportálról letöltött
negyedórás fogyasztási CSV exportokat beolvassa, és **long-term statisztikaként** feltölti a Home
Assistantba. Az így képződő idősor közvetlenül felhasználható az **Energia** irányítópulton, illetve
bármilyen statisztika-alapú kártyán és automatizáláson.

Az integráció teljesen **helyben**, hálózati hozzáférés nélkül működik: csak a fájlrendszerből olvas.

---

## Mit csinál

- Figyeli a beállított import könyvtárat (alapértelmezetten `/config/mvm_next`), és feldolgozza az
  összes ott található `*.csv` fájlt.
- A negyedórás `Vételezett` / `Mért` / `kWh` sorokat óránkénti bontásra összegzi, és az
  `mvm_next:imported_consumption` külső statisztikába tölti fel.
- A teljes idősort minden importnál újraszámolja a legkorábbi ismert órától, így egy utólagos
  korrekció (javított CSV) után a halmozott `sum` érték is konzisztens marad.
- Az MVM Next CSV formátumában szereplő `2026. 08. 01. 00:00` időpontokat `Europe/Budapest`
  időzónában értelmezi, és helyesen kezeli az őszi óraátállítás ismétlődő óráját is (`fold`).
- A már ismert és a lemezen változatlan fájlokat (méret + módosítási idő alapján) kihagyja, csak az
  újakat és a módosítottakat dolgozza fel.
- A feldolgozott fájlok állapotát a Home Assistant `.storage` mappájában tárolja, így újraindítás
  után nem kell mindent újraolvasni.

### Fontos tervezési döntés

A statisztika azonosítója (`mvm_next:imported_consumption`) **szándékosan nem** a mérőóra gyári
számához van kötve. Így egy fizikai mérőóracsere nem indít új, a korábbitól elszakadt statisztika-
sorozatot – a fogyasztási előzmény folytonos marad.

Az import könyvtárban lévő CSV fájlok az **egyetlen forrás**: minden importnál a teljes
statisztika (fogyasztás és költség) újraszámolódik és lecserélődik a fájlok tartalmára.
Így egy javított CSV után nincs elcsúszás vagy dupla adat. Ha egy korábban importált CSV-t
kiveszel a könyvtárból, a hozzá tartozó adatok **kikerülnek** a statisztikából (figyelmeztetés
a naplóba) – ha meg akarod tartani az előzményt, hagyd a fájlokat a könyvtárban.

---

## Telepítés

### HACS (egyéni repository)

1. HACS → jobb felső menü → **Custom repositories**.
2. Add hozzá a repository URL-jét, kategória: **Integration**.
3. Keresd meg és telepítsd az „MVM Next Energy Import” tételt.
4. Indítsd újra a Home Assistantot.

### Kézzel

Másold a [custom_components/mvm_next_energy/](custom_components/mvm_next_energy/) mappát a Home
Assistant konfigurációs könyvtáradba (`<config>/custom_components/mvm_next_energy/`), majd indítsd
újra a Home Assistantot.

Követelmény: Home Assistant **2024.6.0** vagy újabb. Az integráció a `recorder` komponenstől függ.

---

## Beállítás

1. **Beállítások → Eszközök és szolgáltatások → Integráció hozzáadása**.
2. Keresd meg az „MVM Next Energy Import” integrációt.
3. Add meg az **import könyvtárat**, ahová a letöltött CSV fájlokat fogod másolni
   (alapértelmezett: a konfigurációs mappa `mvm_next` almappája, pl. `/homeassistant/mvm_next`).
   A könyvtár létrejön, ha még nem létezik, és később a **Beállítás** menüből módosítható.

---

## Használat

1. Töltsd le az MVM Next ügyfélportálról a negyedórás fogyasztási CSV exportot.
2. Másold a fájlt az import könyvtárba (pl. `/homeassistant/mvm_next/`). Több fájl is lehet
   egyszerre, akár egymást átfedő időszakokkal.

   > **Fájlnevek:** az MVM Next minden exportot ugyanazzal a névvel ad
   > (`meresi_adatok_<gyáriszám>.csv`), ezért egy új hónap felülírná az előzőt. Az integráció
   > ezért importáláskor **automatikusan a benne lévő időszak szerint nevezi át** a fájlokat
   > (`mvm_2026-08.csv`, `mvm_2026-05-01_2026-07-31.csv` stb.), így minden export külön fájl
   > marad. A böngészőből feltöltött fájlokra ez ugyanígy vonatkozik.

   Az integráció **negyedórás bontásban deduplikál**:
   egy adott időponthoz mindig pontosan egy mérési érték tartozik, az adatok soha nem
   adódnak össze. Átfedésnél a **később módosított fájl** értéke érvényes. Ha egy CSV
   véletlenül duplázott sorokat tartalmaz, a felesleges ismétléseket eldobja (a naplóba
   figyelmeztetést ír); az őszi óraátállítás szabályosan ismétlődő óráját viszont helyesen
   megtartja.
3. Indítsd el az importot az alábbi módok valamelyikével:
   - Nyomd meg az **MVM CSV importálása** gombot (button entitás), vagy
   - hívd meg az `mvm_next_energy.import` szolgáltatást.

   Ha nincs kényelmes fájlrendszer-hozzáférésed a Home Assistant géphez, a CSV-t
   közvetlenül a böngészőből is feltöltheted:

   - **Beállítások → Eszközök és szolgáltatások → MVM Next Energy Import →
     „Beállítás" (Configure)** – itt egy fájlfeltöltő felület jelenik meg. Válaszd
     ki a CSV-t, és a feltöltés után a fájl bekerül az import könyvtárba, majd
     azonnal feldolgozásra kerül. Ez a legegyszerűbb feltöltési mód.
   - vagy **Fejlesztői eszközök → Műveletek →
     `MVM Next Energy Import: MVM CSV feltöltése`** (ugyanez művelet formájában,
     automatizáláshoz).
4. Nyisd meg az **Energia** irányítópultot, és add hozzá az
   `mvm_next:imported_consumption` statisztikát a „Hálózati fogyasztás” forráshoz.

### CSV formátum

Az elvárt fejléc és sorformátum (pontosvesszővel elválasztva):

```
Gyári szám;Időpont;Adatpont típus;Státusz;Érték;Mértékegység
2901025000022641;2026. 08. 01. 00:00;Vételezett;Mért;0.039;kWh
```

Csak azok a sorok kerülnek importálásra, ahol az adatpont típusa `Vételezett`, a státusz `Mért`,
és a mértékegység `kWh`. Minden más sor figyelmen kívül marad.

---

## Költségszámítás

A fogyasztás mellé az integráció egy második külső statisztikát is feltölt,
**`mvm_next:imported_cost`** azonosítóval, a Home Assistantban beállított pénznemben
(állítsd **HUF**-ra: *Beállítások → Rendszer → Általános → Pénznem*).

A számítás a **sávos lakossági villamosenergia-árat** követi:

- az adott **naptári évben** a kedvezményes keretig (alapértelmezetten **2523 kWh**) a
  kedvezményes egységár (alapértelmezetten **36,39 Ft/kWh** bruttó),
- a keret felett a piaci egységár (alapértelmezetten **70,104 Ft/kWh** bruttó).

A keret minden naptári év elején nullázódik. A keretet átlépő óra arányosan
kerül megosztásra a két ár között.

A három érték és a be-/kikapcsolás a **Beállítás (Configure) → Áram ára / költségszámítás
beállítása** menüben módosítható; mentés után minden korábbi import automatikusan
újraszámolódik (a CSV-ket nem kell újra beolvasni).

Az Energia irányítópulton a „Hálózati fogyasztás" forrásnál válaszd a **Teljes költségeket
követő entitás használata** opciót, és add meg az `mvm_next:imported_cost` statisztikát.

---

## Szolgáltatás: `mvm_next_energy.import`

Beolvassa az import könyvtárban található CSV fájlokat, és frissíti a long-term statisztikát.

| Mező | Kötelező | Leírás |
|------|----------|--------|
| `file_path` | nem | Egyetlen CSV fájl **kényszerített** újrafeldolgozása akkor is, ha változatlannak tűnik. Lehet abszolút útvonal, vagy az import könyvtárhoz viszonyított relatív fájlnév. Üresen hagyva a teljes könyvtár átvizsgálásra kerül. |

Példa:

```yaml
service: mvm_next_energy.import
data:
  file_path: /homeassistant/mvm_next/mvm_2026-08.csv
```

---

## Szolgáltatás: `mvm_next_energy.upload`

CSV fájl feltöltése közvetlenül a böngészőből (a Home Assistant fájlrendszeréhez való
hozzáférés nélkül). A fájl a benne lévő időszak szerinti néven kerül a beállított import
könyvtárba (pl. `mvm_2026-08.csv`), majd azonnal feldolgozásra kerül.

| Mező | Kötelező | Leírás |
|------|----------|--------|
| `file` | igen | A feltöltendő CSV fájl (a művelet-szerkesztő fájlválasztójával). |

```yaml
service: mvm_next_energy.upload
data:
  file: <a fájlválasztó által adott azonosító>
```

---

## Entitások

Az integráció egy „MVM Next Energy Import” eszközt hoz létre a következő entitásokkal:

| Entitás | Típus | Leírás |
|---------|-------|--------|
| MVM Next Import | `sensor` | Állapota a legutolsó importált negyedóra (helyi idő). Attribútumai: `last_import`, `imported_quarters`, `imported_hours`, `total_energy`, `total_cost`, `meter_serial`, `source_file`, `import_dir`, `statistic_id`, `cost_statistic_id`. |
| MVM CSV importálása | `button` | Megnyomásra átvizsgálja az import könyvtárat és feltölti a frissített statisztikát. |

A feltöltött long-term statisztika azonosítója: **`mvm_next:imported_consumption`**
(mértékegység: `kWh`, energia, halmozott összeggel).

---

## Automatizálási példa

Import minden nap hajnalban, feltéve hogy új fájlt tettél a könyvtárba:

```yaml
automation:
  - alias: MVM Next napi import
    trigger:
      - platform: time
        at: "04:00:00"
    action:
      - service: mvm_next_energy.import
```

---

## Hibakeresés

- **Nem jelenik meg adat az Energia irányítópulton** – ellenőrizd, hogy a szenzor `imported_hours`
  attribútuma nagyobb nullánál, és hogy az `mvm_next:imported_consumption` statisztikát hozzáadtad a
  forráshoz.
- **„nincs feldolgozható CSV”** a naplóban – a fájlok nem a beállított import könyvtárban vannak,
  vagy nem `.csv` kiterjesztésűek.
- Részletes naplózás:

  ```yaml
  logger:
    logs:
      custom_components.mvm_next_energy: debug
  ```

---

## Licenc

Lásd a repository licencfájlját.
