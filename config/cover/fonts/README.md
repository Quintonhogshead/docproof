# Cover Studio fonts

TrueType display faces for Cover Studio's expansion shelf (spec §15.11) —
the ten launch families stay in `../../prep/fonts/` and stay registered; the
registry (`docproof/cover/fonts.py`) reads both roots. All files here were
downloaded from the official Google Fonts repository
(https://github.com/google/fonts, raw files under `ofl/<slug>/`) and are
redistributable; every family's license text (SIL OFL 1.1 in every case)
ships alongside as `<slug>-OFL.txt`, copied from the same repository
directory as its TTFs. Every file reports `fsType=0` (installable —
embedding unrestricted), verified with fontTools at vendoring time.

Static cuts only — no variable fonts (the composer loads faces with Pillow,
which does not instance variable axes). Four §15.11 candidates ship
**variable-only** upstream (no static file and no `static/` subdirectory in
the repo) and were substituted with same-bucket static faces:

- Oswald → **Staatliches**
- Dancing Script → **Pacifico**
- Cinzel → **Cinzel Decorative**
- Archivo → **Poppins**

The names the registry (and the art-direction model) refer to are the fonts'
INTERNAL family names, which are not always the file name:

| Family name | File(s) | License | Source |
|---|---|---|---|
| `Bebas Neue` | BebasNeue-Regular.ttf | OFL 1.1 (`bebasneue-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/bebasneue |
| `Anton` | Anton-Regular.ttf | OFL 1.1 (`anton-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/anton |
| `Staatliches` | Staatliches-Regular.ttf | OFL 1.1 (`staatliches-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/staatliches |
| `Archivo Black` | ArchivoBlack-Regular.ttf | OFL 1.1 (`archivoblack-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/archivoblack |
| `Abril Fatface` | AbrilFatface-Regular.ttf | OFL 1.1 (`abrilfatface-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/abrilfatface |
| `Rozha One` | RozhaOne-Regular.ttf | OFL 1.1 (`rozhaone-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/rozhaone |
| `Yeseva One` | YesevaOne-Regular.ttf | OFL 1.1 (`yesevaone-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/yesevaone |
| `Libre Caslon Display` | LibreCaslonDisplay-Regular.ttf | OFL 1.1 (`librecaslondisplay-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/librecaslondisplay |
| `Alfa Slab One` | AlfaSlabOne-Regular.ttf | OFL 1.1 (`alfaslabone-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/alfaslabone |
| `Zilla Slab` | ZillaSlab-Regular.ttf, ZillaSlab-Italic.ttf, ZillaSlab-Bold.ttf | OFL 1.1 (`zillaslab-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/zillaslab |
| `Great Vibes` | GreatVibes-Regular.ttf | OFL 1.1 (`greatvibes-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/greatvibes |
| `Sacramento` | Sacramento-Regular.ttf | OFL 1.1 (`sacramento-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/sacramento |
| `Pacifico` | Pacifico-Regular.ttf | OFL 1.1 (`pacifico-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/pacifico |
| `Playfair Display SC` | PlayfairDisplaySC-Regular.ttf | OFL 1.1 (`playfairdisplaysc-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/playfairdisplaysc |
| `Cinzel Decorative` | CinzelDecorative-Regular.ttf | OFL 1.1 (`cinzeldecorative-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/cinzeldecorative |
| `Marcellus` | Marcellus-Regular.ttf | OFL 1.1 (`marcellus-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/marcellus |
| `Julius Sans One` | JuliusSansOne-Regular.ttf | OFL 1.1 (`juliussansone-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/juliussansone |
| `Monoton` | Monoton-Regular.ttf | OFL 1.1 (`monoton-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/monoton |
| `Rye` | Rye-Regular.ttf | OFL 1.1 (`rye-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/rye |
| `UnifrakturMaguntia` | UnifrakturMaguntia-Book.ttf | OFL 1.1 (`unifrakturmaguntia-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/unifrakturmaguntia |
| `Fjalla One` | FjallaOne-Regular.ttf | OFL 1.1 (`fjallaone-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/fjallaone |
| `Poppins` | Poppins-Regular.ttf, Poppins-Bold.ttf | OFL 1.1 (`poppins-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/poppins |
| `Space Mono` | SpaceMono-Regular.ttf, SpaceMono-Bold.ttf | OFL 1.1 (`spacemono-OFL.txt`) | https://github.com/google/fonts/tree/main/ofl/spacemono |

UnifrakturMaguntia's single upstream cut is named "Book", not "Regular" —
it is the family's regular weight.

Adding a family: download the TTF **and its license file** from the same
`ofl/<slug>/` (or `apache/<slug>/`) directory, confirm the license is
OFL 1.1/Apache 2.0 and `fsType=0`, register it in
`docproof/cover/fonts.py`, add a row here, and keep
`pyproject.toml`'s `config.cover.fonts` package-data entry intact —
anything missing from package-data is silently absent from the wheel and
FileNotFounds on Fly.
