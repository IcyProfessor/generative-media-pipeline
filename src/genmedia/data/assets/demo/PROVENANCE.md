# Demo fixture provenance

These six character PNGs and three background PNGs were created programmatically
by `scripts/build_demo.py` for this refactor. They are simple geometric test assets,
not copies of the uploaded GIPHY stickers or any branded character artwork.

The fixture provider creates synthetic WAV tones and matching artificial word
metadata. The tones do not sing the displayed captions. This is an engineering
smoke test, not a claim about generative audio quality.

No font files are included. Pillow's installed default text renderer is used unless
the user configures a local font path. The original project code uses the MIT License; this does not license
any separately supplied third-party GIFs, fonts, music, or other media.
