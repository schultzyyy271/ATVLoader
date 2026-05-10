# ATVLoader
Sideload to your AppleTV Device Locally with a Windows PC! NO MAC NO XCODE

## Requirements
- Windows 10/11
- Python 3.10+ (https://python.org — check "Add to PATH")
- Apple TV on the same network

## First Time Setup

1. Download the release zip
2. Extract to a folder
3. Run `SETUP_DEPS.bat` (installs pymobiledevice3)
4. Double-click `ATVLoader.exe`

## Usage

1. **Run the .exe as ADMINISTRATOR!**
2. **Select IPA** — pick your tvOS app
3. **Sign** — load your .p12 certificate + provisioning profile, click Sign
4. **Connect Apple TV:**
   - Click **Scan** to find your Apple TV
   - Select it from the dropdown
   - Click **Pair** (first time only — enter PIN from TV screen)
   - Click **Start Tunnel** (must run as admin)
5. **Install** — pushes the app to your Apple TV

## Notes

- **Run as Administrator** for the tunnel step
- Pairing is one-time — after first pair, just Scan → Tunnel → Install
- Apple TV must be on the same WiFi network
- For first-time pairing: Apple TV → Settings → Remotes and Devices → Remote App and Devices
- Everything runs locally — no external servers

## What you need

| Item | Where to get it |
|------|----------------|
| tvOS .ipa | Build or find one |
| .p12 certificate | Apple Developer Portal or signing services |
| .mobileprovision | Apple Developer Portal (register Apple TV UDID) |

## Credits

- Signing: [zsign](https://github.com/zhlynn/zsign) (MIT License)
- Device communication: [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) (GPL 3.0)

## License

GPL 3.0
