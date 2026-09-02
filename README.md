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

Ha egy korábban importált CSV eltűnik a könyvtárból, a hozzá tartozó, már feltöltött statisztikai
adatok **megmaradnak** a Home Assistantban; csak figyelmeztetés kerül a naplóba.

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
   (alapértelmezett: `/config/mvm_next`). A könyvtár létrejön, ha még nem létezik.

---

## Használat

1. Töltsd le az MVM Next ügyfélportálról a negyedórás fogyasztási CSV exportot.
2. Másold a fájlt az import könyvtárba (pl. `/config/mvm_next/`). Több fájl is lehet egyszerre,
   akár egymást átfedő időszakokkal – az integráció óránként deduplikálja az adatokat.
3. Indítsd el az importot az alábbi módok valamelyikével:
   - Nyomd meg az **MVM CSV importálása** gombot (button entitás), vagy
   - hívd meg az `mvm_next_energy.import` szolgáltatást.
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

## Szolgáltatás: `mvm_next_energy.import`

Beolvassa az import könyvtárban található CSV fájlokat, és frissíti a long-term statisztikát.

| Mező | Kötelező | Leírás |
|------|----------|--------|
| `file_path` | nem | Egyetlen CSV fájl **kényszerített** újrafeldolgozása akkor is, ha változatlannak tűnik. Lehet abszolút útvonal, vagy az import könyvtárhoz viszonyított relatív fájlnév. Üresen hagyva a teljes könyvtár átvizsgálásra kerül. |

Példa:

```yaml
service: mvm_next_energy.import
data:
  file_path: /config/mvm_next/meresi_adatok_augusztus.csv
```

---

## Entitások

Az integráció egy „MVM Next Energy Import” eszközt hoz létre a következő entitásokkal:

| Entitás | Típus | Leírás |
|---------|-------|--------|
| MVM Next Import | `sensor` | Állapota a legutolsó importált negyedóra (helyi idő). Attribútumai: `last_import`, `imported_quarters`, `imported_hours`, `total_energy`, `meter_serial`, `source_file`, `import_dir`, `statistic_id`. |
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
