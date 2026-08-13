# Book-design fonts

TrueType fonts embedded into the book-styled prep output (`book_<name>.docx`)
so the file looks right on machines that don't have them installed. All were
downloaded from https://github.com/google/fonts and are redistributable; every
family's license text is in `licenses/`. Every file here reports `fsType=0`
(installable — embedding unrestricted).

The names the .docx refers to are the fonts' INTERNAL family names, which are
not always the file name:

| File | Family name in Word | License |
|---|---|---|
| Spectral-Regular/Italic/SemiBold/Bold/BoldItalic.ttf | `Spectral` | OFL |
| IMFellEnglish-Regular.ttf | `IM FELL English` | OFL |
| EBGaramond-Regular.ttf | `EB Garamond` | OFL |
| PlayfairDisplay-Regular.ttf | `Playfair Display` | OFL |
| CormorantGaramond-Medium.ttf | `Cormorant Garamond` | OFL |
| Lora-Regular.ttf | `Lora` | OFL |
| Quicksand-Medium.ttf | `Quicksand` | OFL |
| Orbitron-Medium.ttf | `Orbitron` | OFL |
| SpecialElite-Regular.ttf | `Special Elite` | Apache 2.0 |
| PirataOne-Regular.ttf | `Pirata One` | OFL |

Spectral is the body and heading family (it is one of the two faces in the
house-printed interiors this design imitates). The single-weight display faces
are the subject-matter title-page fonts mapped in `../book_design.yaml`.

Variable-font families (EB Garamond, Playfair Display, Cormorant Garamond,
Lora, Quicksand, Orbitron) were instanced to static TTFs with
`fontTools.varLib.instancer` at the weight in the file name, because Word's
support for variable fonts — especially embedded ones — is unreliable.
