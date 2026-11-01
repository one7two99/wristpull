---
name: Parser problem (a file imports wrong or not at all)
about: A FIT file or Gadgetbridge table is parsed incorrectly, or a value differs from what the device shows
title: "[parser] "
labels: parser
---

**Device and software**
- Watch / band and firmware (e.g. fenix 6X Pro 28.02, Amazfit Helio Strap 0.132.24.2):
- Gadgetbridge version (if the file came through the phone):
- wristpull version (`uv run wristpull --version`):

**What is wrong**
What the device or its app shows, and what wristpull stores (table and column). Example:
"sleep.deep_s = 960 but the watch shows 1 h 35 min deep sleep".

**Inspect output**
Run `wristpull inspect <the archived file> --messages <message name or number> --limit 5` on the
raw file under `~/Health/wristpull/raw/...` and paste the relevant part. Please remove serial
numbers and anything else you consider personal; message names, field numbers and a few sample
values are what we need.

```
(paste here)
```

**Reference**
If you can, the same night or activity as Gadgetbridge or the vendor app displays it (numbers are
enough, no screenshots with personal data required).
