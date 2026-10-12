# AstroBake

Automatic processing for deep-sky stacks. Open the linear TIFF straight out of DeepSkyStacker or Siril and AstroBake crops the dead stacking edges, removes the background gradient, balances color and stretches it, with live sliders and a 1:1 preview to fine-tune the result.

| One photo | Stacked | After AstroBake |
|---|---|---|
| ![A single photo of the Andromeda Galaxy: dark blue sky with a faint smudge in the middle](docs/single-photo.jpg) | ![The stack before processing: almost entirely black, the galaxy barely visible](docs/before.jpg) | ![The same stack after AstroBake: the Andromeda Galaxy with its dust lanes and companion galaxies](docs/after.jpg) |

<sub>The Andromeda Galaxy (M31) through a refractor. Left: one of the original photos, as the camera shows it. Middle: the stack straight out of DeepSkyStacker, as it looks in any image viewer. Right: the stack after AstroBake.</sub>

## Download

Grab the file for your computer from the [latest release](../../releases/latest). Nothing else to install.

| System | File | To start |
|---|---|---|
| Windows | `AstroBake-Windows.exe` | Double-click it. |
| macOS (Apple Silicon) | `AstroBake-macOS.zip` | Unzip, move `AstroBake.app` to Applications, double-click it. |
| Linux | `AstroBake-Linux.tar.gz` | Extract, double-click `AstroBake`. |

AstroBake opens in your web browser. It takes a few seconds to start the first time.

### The first time you open it

AstroBake isn't signed with a paid developer certificate, so your system will warn you once:

- **Windows:** "Windows protected your PC". Click **More info**, then **Run anyway**.
- **macOS:** "AstroBake can't be opened" or "Apple could not verify…". Click **Done**, open **System Settings → Privacy & Security**, scroll down and click **Open Anyway** next to AstroBake. (On macOS 14 and older you can instead right-click the app and choose **Open**.)
- **Linux:** if double-clicking does nothing, right-click `AstroBake` → **Properties** → allow it to run as a program.

## Using it

1. Click **Open stack…** and choose your stacked TIFF (16- or 32-bit), or drag it onto the page.
2. Adjust rotation, crop and the tone sliders. Click the preview to choose where the 1:1 detail view looks.
3. Click **Export full resolution**. AstroBake saves `<name>_processed.jpg` next to your original file. Tick **Also save 16-bit TIFF** first if you want a copy for further editing. (For a dragged-in file, browsers don't reveal where it came from, so they go in your Downloads folder.)

When you're done, click **Quit** or just close the browser tab.

Large stacks need roughly 6× their file size in memory (about 2 GB for a 24-megapixel image).

## Running from source

If you have Python 3.9+:

```
python3 astrobake.py
```

If any packages are missing it offers to install them. On Linux systems that don't allow that, it sets up a private virtual environment in `.venv` for you.

It also works from the command line without the UI, which is handy for batch jobs:

```
python3 astrobake.py stacked.tif --batch --stretch 4000 --rotate 12.5
```

The UI shows the exact command for whatever you've dialed in. Run `python3 astrobake.py --help` for all options.

## Building the apps

GitHub Actions builds all three apps (see [.github/workflows/release.yml](.github/workflows/release.yml)). Push a version tag to publish a release with them attached:

```
git tag v1.0.0
git push origin v1.0.0
```

To build one locally: `pip install -r requirements.txt pyinstaller`, then `pyinstaller astrobake.spec`. The result goes in `dist/`.
