# tv3.lv section banner generator

Recreates the Dzīvesstils banner design system (designer original:
`tv3lv_dzivesstils_baneri_preview.pdf`) as a code template and generates
matching banners for the other tv3.lv sections.

Design system, decoded from the original:

- flat muted section color; icon cluster bottom-right in two tonal shades
  of the same hue with white detail cutouts
- white "SADARBĪBĀ AR" swoosh sweeping in from the top-right corner
- white bar: tv3.lv logo left, `/SECTION` + `#SUBSECTION` right
- four sizes: 1000x400, 1000x125 (label block left), 800x500, 800x250

Existing Dzīvesstils colors (do not reuse): #SIEVIETĒM `#c48198`,
#VESELĪBA `#1f9099`, #KULTŪRA `#8f9183`, #CEĻOTPRIEKS `#27847a`,
#DĀRZSunMĀJA `#8aa675`, #DZĪVNIEKI `#b26544`, #RECEPTES `#ecaa1c`,
#AUTO `#767b8d`, #TEHNOLOĢIJAS `#33387f`.

New sections generated here: ZIŅAS (#LATVIJĀ, #ĀRZEMĒS, #EKONOMIKA,
#KRIMINĀLZIŅAS), SPORTS (#HOKEJS, #BASKETBOLS, #FUTBOLS), IZKLAIDE
(#SLAVENĪBAS, #MŪZIKA, #KINO). Add more by appending to `SECTIONS` in
`generate_banners.py` (label, hashtag, slug, color, icon set).

Run:

```bash
python branding/generate_banners.py out_dir
# needs playwright; set PLAYWRIGHT_CHROMIUM to a chromium binary if not installed via playwright
```

Outputs per subsection: 4 PNG sizes + a designer-style preview sheet.

**Before production use**: swap the type for the TV3 brand font and replace
the text-approximated logo with the real logo SVG (both in `banner_html`).
The same template is the base for social carousel end-cards (see the
card_carousel plan in the project discussions).
