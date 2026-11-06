"""chrome_launcher 现场自检（不改动任何东西，只查询）。"""
import os

import chrome_launcher as c

base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "chrome_profiles")
print("console session:", c.current_session_id())
ports = []
for name in sorted(os.listdir(base)):
    d = os.path.join(base, name)
    if not os.path.isdir(d):
        continue
    print(f"profile {name}: {c.find_profile_chromes(d)}")
print("window_status:", c.window_status([9222, 9223, 9224, 9225, 9226, 9227]))
print("port_owner_pid(9222):", c.port_owner_pid(9222))
print("process_session_id(9222 owner):",
      c.process_session_id(c.port_owner_pid(9222) or 0))
