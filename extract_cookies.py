import os
import sys
import glob
import json
import shutil
import sqlite3
import tempfile

import paths

def extract_cookies():
    home = os.path.expanduser("~")
    patterns = []
    for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
        if base:
            patterns.append(os.path.join(base, "Mozilla", "Firefox", "Profiles", "*", "cookies.sqlite"))
    patterns += [
        # Linux / macOS
        os.path.join(home, ".mozilla", "firefox", "*", "cookies.sqlite"),
        os.path.join(home, "snap", "firefox", "common", ".mozilla", "firefox", "*", "cookies.sqlite"),
        os.path.join(home, "Library", "Application Support", "Firefox", "Profiles", "*", "cookies.sqlite"),
    ]
    db_paths = []
    for pat in patterns:
        db_paths.extend(glob.glob(pat))
    
    if not db_paths:
        print("[!] Не найден файл cookies.sqlite")
        return False

    # Choose the most recently modified
    db_paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    src_db = db_paths[0]
    print(f"[*] Используем базу Firefox: {src_db}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_db = os.path.join(tmpdir, "cookies.sqlite")
        shutil.copy2(src_db, tmp_db)
        wal = src_db + "-wal"
        if os.path.exists(wal):
            shutil.copy2(wal, tmp_db + "-wal")

        con = sqlite3.connect(tmp_db)
        cur = con.cursor()
        query = """
            SELECT name, value, host, path, isSecure, isHttpOnly, expiry
            FROM moz_cookies
            WHERE host LIKE '%yandex%'
        """
        cur.execute(query)
        rows = cur.fetchall()
        con.close()

    if not rows:
        print("[-] Куки для yandex не найдены. Вы уже вошли в аккаунт в Firefox?")
        return False

    cookies_list = []
    for name, val, host, path, is_sec, is_http_only, expiry in rows:
        cookies_list.append({
            "name": name,
            "value": val,
            "domain": host,
            "path": path,
            "secure": bool(is_sec),
            "httpOnly": bool(is_http_only),
            "expirationDate": expiry
        })

    target_file = str(paths.COOKIES_FILE)
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(cookies_list, f, indent=2, ensure_ascii=False)

    print(f"[+] Успешно извлечено {len(cookies_list)} кук для Yandex.")
    print(f"[+] Сохранено в {target_file}")
    
    # Check essential cookies
    cookie_dict = {c["name"]: c["value"] for c in cookies_list}
    has_session = "Session_id" in cookie_dict or "sessionid2" in cookie_dict
    print(f"[*] Присутствует Session_id: {has_session}")
    return has_session

def main() -> int:
    return 0 if extract_cookies() else 1


if __name__ == "__main__":
    raise SystemExit(main())
