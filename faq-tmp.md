**Frequently Asked Questions**

❓ **What is Stats++?**
A local analytics dashboard for OOTP leagues on StatsPlus. Player evaluations, prospect rankings, contract analysis, and trade tools — all calibrated from your league's own data.
🔗 https://github.com/tfalsone/statsplusplus

❓ **What leagues does it support?**
Any OOTP league hosted on StatsPlus. Multi-league support included.

❓ **How do ratings/grades work?**
Independent evaluation from the game's OVR/POT. Tool-weighted composites, FV grades on the 20-80 scale, and surplus values in dollars. Models calibrate from each league's data.

❓ **How do I install it?**
1. Install Python 3.10+ from https://www.python.org/downloads/ (check "Add to PATH")
2. Download the latest zip: https://github.com/tfalsone/statsplusplus/releases/latest
3. Extract and run the launcher:
   • **Windows:** Double-click `start.bat`
   • **Mac/Linux:** Run `./start.sh`
4. Browser opens to setup wizard — paste your StatsPlus cookie and go

No git or programming knowledge needed.

❓ **How do I get my StatsPlus cookie?**
Log in to statsplus.net → F12 → Application → Cookies → copy `sessionid` and `csrftoken`. Format: `sessionid=XXX;csrftoken=XXX`. Refresh your cookie if data pulls start failing.

❓ **How often should I refresh?**
After each sim advance. Click ⟳ in the nav bar (2-3 min).

❓ **How do I update?**
Download the latest zip from Releases and extract over your existing folder. Your league data won't be overwritten.

❓ **Bug / feature idea?**
Post in ⁠bug-reports or ⁠feature-requests with screenshots if possible.
