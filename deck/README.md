# Submission deck

`index.html` is the deck. Open it in a browser; arrow keys, space, PageUp/PageDown
and Home/End move between slides.

`Gatekeeper.pptx` is the same twelve slides as full-bleed 1920x1080 images, sized
to a 16:9 stage. **Google Slides imports it directly** — File → Import slides →
Upload. The text is flattened into the images, so edit `index.html` and re-render
rather than editing the deck in Slides.

`slides/` holds the individual PNGs, for anywhere a single frame is more useful
than a deck.

## Re-rendering after an edit

Edit `index.html`, then from the repo root:

```sh
python3 - <<'PY'
s = open('deck/index.html').read()
s = s.replace("html{scroll-behavior:smooth}", "html{scroll-behavior:auto}")
s = s.replace("  deck.focus();",
  "  var m=location.search.match(/slide=(\\d+)/);\n"
  "  if(m){deck.scrollTop=slides[Math.max(0,Math.min(slides.length-1,+m[1]-1))].offsetTop;}")
s = s.replace("</style>", ".prog{display:none!important}\n"
                          ".deck:focus-visible,.deck:focus{outline:none!important}\n</style>")
open('/tmp/deck-render.html','w').write(s)
PY

CH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
for i in $(seq 1 12); do
  "$CH" --headless --disable-gpu --hide-scrollbars --virtual-time-budget=6000 \
    --window-size=1920,1080 \
    --screenshot="deck/slides/slide-$(printf %02d $i).png" \
    "file:///tmp/deck-render.html?slide=$i"
done
```

`--virtual-time-budget` is not optional: without it headless Chrome screenshots
before the Google Fonts `@import` resolves and silently falls back to serif.
The render copy also hides the progress bar and focus ring, which are interactive
chrome that should not appear in a static frame.

Rebuild the pptx with `python-pptx` (see the project history for the script).
