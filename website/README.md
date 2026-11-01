# website/

A static one-page site that presents wristpull (`index.html`, no build step, fonts from Google Fonts,
everything else inline). Screenshots are referenced from `../screenshots/`; when deploying the page on
its own, copy that folder next to it and adjust the paths, or serve the repository root.

Open locally: `python3 -m http.server -d . 8000` from the repository root, then
<http://localhost:8000/website/>.
