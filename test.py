import network
import socket
import time
import gc

try:
    import ssl
except ImportError:
    import ussl as ssl


HOST = "www.youtube.com"
PORT = 443
PATH = "/"
SCAN_RETRIES = 3
SCAN_DELAY_SECONDS = 2
CONNECT_RETRIES = 100
CONNECT_DELAY_SECONDS = 0.1
SOCKET_TIMEOUT_SECONDS = 15
MAX_HEADER_BYTES = 8192
MAX_REDIRECTS = 3

sta_if = network.WLAN(network.STA_IF)


def ensure_station_active():
    if not sta_if.active():
        sta_if.active(True)
        time.sleep(1)
    return sta_if


def decode_text(value):
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeError:
            return value.decode("utf-8", "ignore")
    return str(value)


def current_ssid():
    try:
        return decode_text(sta_if.config("essid"))
    except Exception:
        return ""


def url_encode(text):
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    encoded = []

    for ch in text:
        if ch in safe:
            encoded.append(ch)
        elif ch == " ":
            encoded.append("+")
        else:
            for byte in ch.encode("utf-8"):
                encoded.append("%{:02X}".format(byte))

    return "".join(encoded)


def build_youtube_path():
    query = input("Enter YouTube search text, or press Enter for home page: ").strip()
    if not query:
        return PATH
    return "/results?search_query={}".format(url_encode(query))


def scan_networks():
    ensure_station_active()
    found = {}

    for attempt in range(1, SCAN_RETRIES + 1):
        print("Scanning Wi-Fi networks... attempt {}/{}".format(attempt, SCAN_RETRIES))

        try:
            networks = sta_if.scan()
        except OSError as error:
            print("Scan failed:", error)
            networks = []

        for info in networks:
            ssid = decode_text(info[0]).strip()
            channel = info[2]
            rssi = info[3]

            if not ssid:
                continue

            saved = found.get(ssid)
            if saved is None or rssi > saved["rssi"]:
                found[ssid] = {
                    "ssid": ssid,
                    "channel": channel,
                    "rssi": rssi,
                }

        if found:
            break

        if attempt < SCAN_RETRIES:
            print("No visible Wi-Fi networks found. Retrying...")
            time.sleep(SCAN_DELAY_SECONDS)

    network_list = sorted(found.values(), key=lambda item: item["rssi"], reverse=True)

    if not network_list:
        print("No visible Wi-Fi networks found after {} attempts.".format(SCAN_RETRIES))
        print("ESP32-S3 supports 2.4 GHz Wi-Fi only. 5 GHz or hidden SSIDs may not appear.")
        return []

    for index, info in enumerate(network_list, 1):
        print(
            "{}: {} (RSSI {}, ch {})".format(
                index,
                info["ssid"],
                info["rssi"],
                info["channel"],
            )
        )

    return network_list


def prompt_wifi_credentials():
    while True:
        networks = scan_networks()
        print("")

        if networks:
            selected = input("Enter Wi-Fi number, SSID, or r to rescan: ").strip()
            if selected.lower() == "r":
                continue

            if selected.isdigit():
                index = int(selected) - 1
                if 0 <= index < len(networks):
                    ssid = networks[index]["ssid"]
                else:
                    print("Invalid Wi-Fi number.")
                    continue
            else:
                ssid = selected
        else:
            ssid = input("Enter Wi-Fi SSID manually, or press Enter to rescan: ").strip()
            if not ssid:
                continue

        password = input("Enter Wi-Fi password: ")
        return ssid, password


def do_connect(ssid, password):
    ensure_station_active()

    if sta_if.isconnected() and current_ssid() == ssid:
        print("Wi-Fi already connected:", sta_if.ifconfig())
        return True

    if sta_if.isconnected():
        try:
            sta_if.disconnect()
            time.sleep(1)
        except OSError:
            pass

    print("Trying to connect to {}...".format(ssid))
    sta_if.connect(ssid, password)

    connected = False
    for retry in range(CONNECT_RETRIES):
        connected = sta_if.isconnected()
        if connected:
            break

        time.sleep(CONNECT_DELAY_SECONDS)
        if retry % 10 == 9:
            print(".", end="")

    print("")

    if connected:
        print("Connected. Network config:", sta_if.ifconfig())
        return True

    print("Failed. Not connected to:", ssid)
    try:
        print("Wi-Fi status:", sta_if.status())
    except Exception:
        pass
    return False


def parse_url(url):
    secure = True
    port = 443

    if url.startswith("https://"):
        rest = url[8:]
    elif url.startswith("http://"):
        secure = False
        port = 80
        rest = url[7:]
    else:
        rest = url

    slash_index = rest.find("/")
    if slash_index == -1:
        host_port = rest
        path = "/"
    else:
        host_port = rest[:slash_index]
        path = rest[slash_index:]

    if ":" in host_port:
        host, port_text = host_port.split(":", 1)
        port = int(port_text)
    else:
        host = host_port

    return secure, host, port, path


def resolve_redirect_url(location, current_host, current_path):
    if location.startswith("http://") or location.startswith("https://"):
        return location

    if location.startswith("/"):
        return "https://{}{}".format(current_host, location)

    base = current_path.rsplit("/", 1)[0]
    if not base:
        base = ""
    return "https://{}{}/{}".format(current_host, base, location)


def open_http_stream(url):
    secure, host, port, path = parse_url(url)
    gc.collect()
    print("Resolving host:", host)
    addr = socket.getaddrinfo(host, port)[0][-1]
    print("Connecting to:", addr)

    raw_sock = socket.socket()
    raw_sock.settimeout(SOCKET_TIMEOUT_SECONDS)
    raw_sock.connect(addr)

    if secure:
        gc.collect()
        try:
            sock = ssl.wrap_socket(raw_sock, server_hostname=host)
        except TypeError:
            sock = ssl.wrap_socket(raw_sock)
    else:
        sock = raw_sock

    request = (
        "GET {} HTTP/1.0\r\n"
        "Host: {}\r\n"
        "User-Agent: Mozilla/5.0 (Linux; Android 14; ESP32-S3) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36\r\n"
        "Accept: text/html,application/xhtml+xml\r\n"
        "Accept-Language: en-US,en;q=0.9\r\n"
        "Accept-Encoding: identity\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).format(path, host)

    sock.write(request.encode())

    header_buffer = b""
    while b"\r\n\r\n" not in header_buffer:
        chunk = sock.read(256)
        if chunk is None:
            continue
        if not chunk:
            break
        header_buffer += chunk
        if len(header_buffer) > MAX_HEADER_BYTES:
            raise OSError("HTTP headers too large")

    header_end = header_buffer.find(b"\r\n\r\n")
    if header_end == -1:
        raise OSError("Incomplete HTTP headers")

    raw_headers = header_buffer[:header_end]
    body_start = header_buffer[header_end + 4 :]
    lines = raw_headers.split(b"\r\n")

    status_line = decode_text(lines[0]).strip()
    parts = status_line.split(" ", 2)
    status_code = 0
    if len(parts) >= 2:
        try:
            status_code = int(parts[1])
        except ValueError:
            status_code = 0

    headers = {}
    for line in lines[1:]:
        if b":" not in line:
            continue
        key, value = line.split(b":", 1)
        headers[decode_text(key).lower()] = decode_text(value).strip()

    return sock, host, path, status_line, status_code, headers, body_start


def print_body_stream(sock, body_start):
    print("\n--- YOUTUBE HTML START ---\n")

    if body_start:
        try:
            print(body_start.decode("utf-8"), end="")
        except UnicodeError:
            print(body_start.decode("utf-8", "ignore"), end="")

    while True:
        chunk = sock.read(512)
        if chunk is None:
            continue
        if not chunk:
            break

        try:
            print(chunk.decode("utf-8"), end="")
        except UnicodeError:
            print(chunk.decode("utf-8", "ignore"), end="")

    print("\n\n--- YOUTUBE HTML END ---")


def fetch_and_print_html(url):
    current_url = url

    for _ in range(MAX_REDIRECTS + 1):
        sock = None
        try:
            sock, host, path, status_line, status_code, headers, body_start = open_http_stream(current_url)
            print("HTTP status:", status_line)

            location = headers.get("location")
            if status_code in (301, 302, 303, 307, 308) and location:
                print("Redirecting to:", location)
                current_url = resolve_redirect_url(location, host, path)
                sock.close()
                continue

            content_type = headers.get("content-type", "")
            if content_type:
                print("Content-Type:", content_type)

            content_encoding = headers.get("content-encoding", "")
            if content_encoding and content_encoding.lower() != "identity":
                print("Unsupported content encoding:", content_encoding)
                return

            print_body_stream(sock, body_start)
            return
        except OSError as error:
            print("Fetch error:", error)
            return
        finally:
            if sock is not None:
                sock.close()

    print("Too many redirects while opening YouTube.")


def main():
    ssid, password = prompt_wifi_credentials()
    if not do_connect(ssid, password):
        return
    path = build_youtube_path()
    fetch_and_print_html("https://{}{}".format(HOST, path))


main()
