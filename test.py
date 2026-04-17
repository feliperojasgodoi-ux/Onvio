"""
Onvio Platform – Selenium automation for task/company management.

Refactored for:
  • Proper logging instead of print()
  • Configuration dataclass for all tunables
  • Page-Object helpers to reduce nesting and duplication
  • Fixed control-flow bugs (`found` never set, unreachable fuzzy code)
  • Removed hardcoded credentials (env-vars only)
  • Cross-platform chromedriver detection
  • Type hints throughout
"""

import json
import logging
import os
import re
import shutil
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import difflib

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """Central place for every tunable value used by the automation."""

    url: str = "https://onvio.com.br/login/#/"

    # Timeouts (seconds)
    default_wait: int = 20
    short_wait: int = 8
    new_tab_wait: int = 10

    # Retry / polling
    max_search_retries: int = 3
    ui_settle_delay: float = 0.5
    search_settle_delay: float = 0.6

    # Fuzzy-matching
    fuzzy_threshold: float = 0.65
    max_query_tokens: int = 3

    # Sidebar
    sidebar_offset: int = 220

    # Screenshots
    screenshot_dir: str = "screenshots"

    # Query-builder stop-words
    corp_stop: set = field(default_factory=lambda: {
        "ltda", "l.t.d.a", "me", "epp", "sa", "s.a", "s/a",
        "ei", "eireli", "ss", "s.s", "matriz", "filial",
        "holding", "holdings",
    })
    generic_stop: set = field(default_factory=lambda: {
        "comercio", "comércio", "servicos", "serviço",
        "servicos.", "serviço.", "industria", "indústria",
        "comercial", "empresa", "grupo", "e", "&",
    })

    @property
    def stopwords(self) -> set:
        return self.corp_stop | self.generic_stop


CFG = Config()

# ---------------------------------------------------------------------------
# Text / Query helpers
# ---------------------------------------------------------------------------

def strip_accents(text: str) -> str:
    """Remove diacritical marks from *text*."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def tokenize(name: str, stopwords: Optional[set] = None) -> List[str]:
    """Lowercase, strip accents, remove stop-words, deduplicate."""
    if stopwords is None:
        stopwords = CFG.stopwords
    text = re.sub(r"[^a-z0-9]+", " ", strip_accents(name).lower())
    seen: set = set()
    tokens: List[str] = []
    for tok in text.split():
        if tok and tok not in stopwords and tok not in seen:
            seen.add(tok)
            tokens.append(tok)
    return tokens


def build_query_basic(full_name: str, max_tokens: int = 3) -> str:
    """Return a short search query (up to *max_tokens* non-digit words)."""
    tokens = tokenize(full_name)
    core = [t for t in tokens if not t.isdigit()] or tokens
    return " ".join(core[:max_tokens])


def build_query_candidates(full_name: str, max_tokens: int = 3) -> List[str]:
    """Return queries from most specific (longest) to least specific."""
    base_tokens = build_query_basic(full_name, max_tokens=max_tokens).split()
    candidates = [" ".join(base_tokens[:k]) for k in range(len(base_tokens), 0, -1)]
    return list(dict.fromkeys(candidates))


def build_query_smart(
    full_name: str,
    all_company_names: Sequence[str],
    max_tokens: int = 3,
) -> str:
    """Pick the rarest tokens across *all_company_names* for a more unique query."""
    freq: Counter = Counter(
        tok for name in all_company_names for tok in tokenize(name)
    )
    tokens = tokenize(full_name)
    tokens_sorted = sorted(tokens, key=lambda t: (freq.get(t, 0), len(t)))
    core = [t for t in tokens_sorted if not t.isdigit()] or tokens_sorted
    return " ".join(core[:max_tokens])


# ---------------------------------------------------------------------------
# Selenium helpers
# ---------------------------------------------------------------------------

Locator = Tuple[str, str]  # (By.*, value)


def _safe_click(driver: WebDriver, element: WebElement) -> None:
    """Click *element*, falling back to a JS click on interception."""
    try:
        element.click()
    except (ElementClickInterceptedException, Exception):
        driver.execute_script("arguments[0].click();", element)


def find_and_click(
    wait: WebDriverWait,
    driver: WebDriver,
    selectors: Sequence[Locator],
) -> bool:
    """Try each locator in *selectors* until one is clickable, then click it."""
    for selector in selectors:
        try:
            element = wait.until(EC.element_to_be_clickable(selector))
            driver.execute_script("arguments[0].scrollIntoView(true);", element)
            _safe_click(driver, element)
            return True
        except (TimeoutException, NoSuchElementException):
            continue
        except Exception as exc:
            logger.debug("find_and_click: selector %s raised %r", selector, exc)
            continue
    return False


def find_input_and_type(
    wait: WebDriverWait,
    driver: WebDriver,
    candidates: Sequence[Locator],
    value: str,
) -> bool:
    """Locate an input element and type *value* into it."""
    for candidate in candidates:
        try:
            element = wait.until(EC.element_to_be_clickable(candidate))
            element.clear()
            element.send_keys(value)
            return True
        except (TimeoutException, NoSuchElementException):
            continue
        except Exception as exc:
            logger.debug("find_input_and_type: candidate %s raised %r", candidate, exc)
            continue
    return False


def try_select_checkbox(
    driver: WebDriver,
    row: WebElement,
) -> bool:
    """Find a checkbox inside *row* and select it if not already selected.

    Returns True if a checkbox was found and is now selected.
    """
    checkbox_selectors = [
        "input[type='checkbox']",
        ".all-check-box input[type='checkbox']",
    ]
    for css in checkbox_selectors:
        try:
            checkbox = row.find_element(By.CSS_SELECTOR, css)
            driver.execute_script("arguments[0].scrollIntoView(true);", checkbox)
            if not checkbox.is_selected():
                _safe_click(driver, checkbox)
            return True
        except NoSuchElementException:
            continue
        except Exception as exc:
            logger.warning("Checkbox interaction failed: %r", exc)
    return False


def save_screenshot(driver: WebDriver, name: str) -> None:
    """Save a screenshot under the configured directory."""
    directory = Path(CFG.screenshot_dir)
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w.\-]", "_", name)[:120]
    path = directory / f"{safe_name}.png"
    try:
        driver.save_screenshot(str(path))
        logger.info("Screenshot saved: %s", path)
    except Exception as exc:
        logger.warning("Could not save screenshot %s: %r", path, exc)


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------

def _resolve_chromedriver() -> Optional[str]:
    """Return chromedriver path (local file or system PATH)."""
    local = Path(__file__).parent / "chromedriver.exe"
    if local.exists():
        return str(local)
    local_unix = Path(__file__).parent / "chromedriver"
    if local_unix.exists():
        return str(local_unix)
    if shutil.which("chromedriver"):
        return None  # let Selenium find it on PATH
    return None


def create_driver(headless: bool = False) -> WebDriver:
    """Build a Chrome WebDriver with sensible defaults."""
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")

    chromedriver_path = _resolve_chromedriver()
    if chromedriver_path:
        service = Service(chromedriver_path)
        return webdriver.Chrome(service=service, options=options)
    return webdriver.Chrome(options=options)


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------

INITIAL_BTN_SELECTORS: List[Locator] = [
    (By.ID, "trauth-continue-signin-bt"),
    (By.ID, "trauth-continue-signin-btn"),
    (By.NAME, "action"),
    (By.XPATH, "//button[contains(., 'Entrar')]"),
    (By.CSS_SELECTOR, "button[type='submit']"),
]

EMAIL_INPUT_SELECTORS: List[Locator] = [
    (By.NAME, "username"),
    (By.NAME, "email"),
    (By.ID, "username"),
    (By.CSS_SELECTOR, "input[type='email']"),
    (By.CSS_SELECTOR, "input[type='text']"),
]

PASSWORD_INPUT_SELECTORS: List[Locator] = [
    (By.NAME, "password"),
    (By.ID, "password"),
    (By.CSS_SELECTOR, "input[type='password']"),
]

SUBMIT_BTN_SELECTORS: List[Locator] = [
    (By.CSS_SELECTOR, "button[type='submit']"),
    (By.XPATH, "//button[contains(., 'Entrar') or contains(., 'Login')]"),
    (By.NAME, "action"),
]


def perform_login(driver: WebDriver, wait: WebDriverWait) -> None:
    """Execute the full Onvio login flow."""
    username = os.environ.get("WORK_USER")
    password = os.environ.get("WORK_PASS")
    if not username or not password:
        raise EnvironmentError(
            "WORK_USER and WORK_PASS environment variables must be set."
        )

    driver.get(CFG.url)

    # Initial "Entrar" / continue button (may or may not exist)
    find_and_click(wait, driver, INITIAL_BTN_SELECTORS)

    # Email
    if not find_input_and_type(wait, driver, EMAIL_INPUT_SELECTORS, username):
        raise RuntimeError("Email input field not found – check selectors.")

    # Advance past email step
    find_and_click(wait, driver, INITIAL_BTN_SELECTORS)

    # Password
    if not find_input_and_type(wait, driver, PASSWORD_INPUT_SELECTORS, password):
        raise RuntimeError("Password input field not found – check selectors.")

    # Submit
    if not find_and_click(wait, driver, SUBMIT_BTN_SELECTORS):
        raise RuntimeError("Login submit button not found – check selectors.")

    logger.info("Login submitted successfully.")


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

MENU_BTN_SELECTORS: List[Locator] = [
    (By.CSS_SELECTOR, "button[aria-label='Menu']"),
    (By.ID, "bm-header-app-menu-toggle"),
]

PROCESSOS_BTN_SELECTORS: List[Locator] = [
    (By.XPATH, "//a[contains(., 'Processos')]"),
    (By.LINK_TEXT, "Processos"),
]


def navigate_to_processos(driver: WebDriver, wait: WebDriverWait) -> None:
    """Open Menu → Processos (handles new tab)."""
    if not find_and_click(wait, driver, MENU_BTN_SELECTORS):
        raise RuntimeError("Menu button not found – check selectors.")

    handles_before = set(driver.window_handles)
    if not find_and_click(wait, driver, PROCESSOS_BTN_SELECTORS):
        raise RuntimeError("'Processos' link not found – check selectors.")

    # Switch to the new tab opened by Processos
    try:
        WebDriverWait(driver, CFG.new_tab_wait).until(
            lambda d: len(d.window_handles) > len(handles_before)
        )
        new_handles = [h for h in driver.window_handles if h not in handles_before]
        driver.switch_to.window(new_handles[0] if new_handles else driver.window_handles[-1])
    except TimeoutException:
        driver.switch_to.window(driver.window_handles[-1])

    logger.info("Switched to Processos tab: %s", driver.current_url)


def try_expand_sidebar(driver: WebDriver, wait: WebDriverWait) -> bool:
    """Attempt to slide/expand the sidebar so menu items are visible."""
    sidebar_candidates: List[Locator] = [
        (By.CSS_SELECTOR, "div[class*='c-ikMfhs']"),
        (By.CSS_SELECTOR, "div[class*='app-sidebar']"),
        (By.CSS_SELECTOR, "div[role='navigation']"),
        (By.XPATH, "//div[contains(@class,'sidebar') or contains(@class,'menu')][1]"),
    ]
    for selector in sidebar_candidates:
        try:
            element = wait.until(EC.presence_of_element_located(selector))
            ActionChains(driver) \
                .move_to_element(element) \
                .click_and_hold(element) \
                .move_by_offset(CFG.sidebar_offset, 0) \
                .release() \
                .perform()
            time.sleep(CFG.ui_settle_delay)
            return True
        except Exception:
            continue

    # JS fallback
    try:
        driver.execute_script(
            "var el = document.querySelector("
            "'div[class*=\"sidebar\"], div[role=\"navigation\"]');"
            "if (el) { el.classList.remove('collapsed');"
            " el.style.transform='translateX(0px)'; }"
        )
        time.sleep(CFG.ui_settle_delay)
        return True
    except Exception:
        return False


def navigate_to_gerenciar(driver: WebDriver, wait: WebDriverWait) -> None:
    """Navigate Configure → Gerenciar inside the Processos page."""
    configure_selectors: List[Locator] = [
        (By.CSS_SELECTOR, "div[data-qe-id='gestta_menu-configure']"),
        (By.XPATH, "//div[normalize-space()='Configure']"),
        (By.PARTIAL_LINK_TEXT, "Config"),
    ]
    if not find_and_click(wait, driver, configure_selectors):
        raise RuntimeError("'Configure' menu item not found – check selectors.")

    time.sleep(CFG.ui_settle_delay)

    gerenciar_selectors: List[Locator] = [
        (By.XPATH, "//a[@id='gestta_menu-tarefas-recorrentes-gerenciar']"),
        (By.XPATH, "//a[normalize-space()='Gerenciar']"),
        (By.XPATH, "//div[normalize-space()='Gerenciar']"),
        (By.XPATH, "//button[normalize-space()='Gerenciar']"),
        (By.XPATH, "//a[contains(., 'Gerenciar')]"),
    ]
    if not find_and_click(wait, driver, gerenciar_selectors):
        raise RuntimeError("'Gerenciar' menu item not found – check selectors.")

    logger.info("Navigated to Gerenciar.")


# ---------------------------------------------------------------------------
# Task / Company search
# ---------------------------------------------------------------------------

SEARCH_INPUT_SELECTORS: List[Locator] = [
    (By.CSS_SELECTOR, "input[placeholder*='Pesquisar por nome']"),
    (By.CSS_SELECTOR, "input[placeholder*='Pesquisar']"),
    (By.CSS_SELECTOR, "input.form-control"),
    (By.CSS_SELECTOR, "input[type='search']"),
]

CLIENT_SEARCH_INPUT_SELECTORS: List[Locator] = [
    (By.CSS_SELECTOR, "input[ng-model*='foreignListFilter']"),
    (By.CSS_SELECTOR, "input[ng-model*='foreignCtrl.foreignListFilter']"),
    (By.CSS_SELECTOR, "input[placeholder*='Pesquisar por nome']"),
    (By.CSS_SELECTOR, "input[placeholder*='Pesquisar']"),
    (By.CSS_SELECTOR, "input.form-control"),
    (By.CSS_SELECTOR, "input[type='search']"),
]

CLIENTES_BTN_SELECTORS: List[Locator] = [
    (By.XPATH, "//a[contains(@class,'btn tr-btn-group ng-scope') and contains(@href,'/customer')]"),
    (By.LINK_TEXT, "Clientes"),
    (By.XPATH, "//a[contains(., 'Clientes')]"),
]

ADD_BTN_SELECTORS: List[Locator] = [
    (By.CSS_SELECTOR, "button.btn.btn-block.btn-success"),
    (By.XPATH, "//button[normalize-space()='Adicionar']"),
    (By.XPATH, "//button[contains(., 'Adicionar')]"),
]

RESULT_ROWS_XPATH = (
    "//table//tbody//tr"
    " | //ul//li"
    " | //div[contains(@class,'foreign-list-results')]"
    "//div[contains(@class,'row') or contains(@class,'item')]"
    " | //div[@class='ibox-content']//div[contains(@class,'row')]"
)


def _find_search_input(
    driver: WebDriver,
    selectors: Sequence[Locator],
    timeout: int = 8,
) -> Optional[WebElement]:
    """Locate a visible & clickable search input."""
    for selector in selectors:
        try:
            return WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable(selector)
            )
        except (TimeoutException, NoSuchElementException):
            continue
    return None


def _clear_and_type(
    driver: WebDriver,
    element: WebElement,
    text: str,
) -> None:
    """Clear an input and type *text*, with JS fallback for clearing."""
    try:
        element.clear()
    except Exception:
        driver.execute_script("arguments[0].value = '';", element)
    element.send_keys(text)


def _collect_visible_results(
    driver: WebDriver,
    max_rows: int = 10,
) -> List[Tuple[WebElement, str]]:
    """Return up to *max_rows* (element, display_text) from search results."""
    rows = driver.find_elements(By.XPATH, RESULT_ROWS_XPATH)
    candidates: List[Tuple[WebElement, str]] = []
    for row in rows[:max_rows]:
        text = row.text.strip()
        try:
            anchor = row.find_element(By.TAG_NAME, "a")
            text = anchor.text.strip() or text
        except NoSuchElementException:
            pass
        if text:
            candidates.append((row, text))
    return candidates


def _select_best_match(
    driver: WebDriver,
    candidates: List[Tuple[WebElement, str]],
    target_norm: str,
) -> bool:
    """Use fuzzy matching to find and click the best candidate.

    Returns True if a match above the threshold was selected.
    """
    best_element: Optional[WebElement] = None
    best_text = ""
    best_score = 0.0

    for element, text in candidates:
        norm = strip_accents(text).lower()
        score = difflib.SequenceMatcher(None, target_norm, norm).ratio()
        if score > best_score:
            best_score = score
            best_element = element
            best_text = text

    logger.debug(
        "Fuzzy match: candidates=%d best='%s' score=%.2f",
        len(candidates), best_text, best_score,
    )

    if best_element is None or best_score < CFG.fuzzy_threshold:
        return False

    if not try_select_checkbox(driver, best_element):
        driver.execute_script("arguments[0].scrollIntoView(true);", best_element)
        _safe_click(driver, best_element)

    logger.info("Selected (fuzzy %.2f): '%s'", best_score, best_text)
    return True


def search_and_click_task(
    driver: WebDriver,
    wait: WebDriverWait,
    label: str,
) -> bool:
    """Search for *label* in the task list and click it.

    Returns True if the task was found and navigated to.
    """
    for attempt in range(1, CFG.max_search_retries + 1):
        input_el = _find_search_input(driver, SEARCH_INPUT_SELECTORS)
        if not input_el:
            logger.warning("Task search input not found (attempt %d).", attempt)
            continue

        _clear_and_type(driver, input_el, label)
        time.sleep(CFG.search_settle_delay)
        try:
            input_el.send_keys(Keys.ENTER)
        except Exception:
            pass
        time.sleep(CFG.search_settle_delay)

        # Look for exact match
        exact_xpath = f"//a[normalize-space()={json.dumps(label)}]"
        links = driver.find_elements(By.XPATH, exact_xpath)

        # Fallback: case-insensitive match
        if not links:
            ci_xpath = (
                f"//a[translate(normalize-space(.),"
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
                f" = {json.dumps(label.lower())}]"
            )
            links = driver.find_elements(By.XPATH, ci_xpath)

        if not links:
            logger.info("Task '%s': no match on attempt %d.", label, attempt)
            continue

        link = links[0]
        logger.debug("Clicking task link: text='%s' href='%s'",
                      link.text, link.get_attribute("href"))
        driver.execute_script("arguments[0].scrollIntoView(true);", link)

        pre_url = driver.current_url
        _safe_click(driver, link)

        try:
            WebDriverWait(driver, CFG.new_tab_wait).until(
                lambda d: d.current_url != pre_url
            )
        except TimeoutException:
            time.sleep(1)

        logger.info("Task '%s' found and clicked.", label)
        return True

    return False


def open_clients_tab(driver: WebDriver, wait: WebDriverWait) -> bool:
    """Click the 'Clientes' tab in the task detail page."""
    if not find_and_click(wait, driver, CLIENTES_BTN_SELECTORS):
        logger.warning("'Clientes' button not found.")
        return False

    # Allow the Clientes panel to load
    time.sleep(CFG.short_wait)

    # Click again if needed (some UIs require a second click after tab loads)
    find_and_click(wait, driver, CLIENTES_BTN_SELECTORS)
    return True


def search_and_select_company(
    driver: WebDriver,
    clients_input: WebElement,
    company: str,
    task_map: dict,
    task_key: str,
) -> bool:
    """Search for *company* in the clients panel and select it.

    Returns True if the company was successfully selected.
    """
    all_companies = [c for companies in task_map.values() for c in companies]
    query_candidates = build_query_candidates(company, max_tokens=CFG.max_query_tokens)
    smart_query = (
        build_query_smart(company, all_companies, max_tokens=CFG.max_query_tokens)
        if all_companies else None
    )
    if smart_query and smart_query not in query_candidates:
        query_candidates.append(smart_query)

    target_norm = strip_accents(company).lower()

    for query in query_candidates:
        _clear_and_type(driver, clients_input, query)
        time.sleep(CFG.search_settle_delay)
        try:
            clients_input.send_keys(Keys.ENTER)
        except Exception:
            pass
        time.sleep(CFG.search_settle_delay)

        candidates = _collect_visible_results(driver)
        if not candidates:
            logger.info("    query '%s' → 0 results", query)
            continue

        # Single candidate → accept if fuzzy score is reasonable
        if len(candidates) == 1:
            row, text = candidates[0]
            norm = strip_accents(text).lower()
            score = difflib.SequenceMatcher(None, target_norm, norm).ratio()
            logger.debug("    Single result '%s' score=%.2f", text, score)
            if score >= CFG.fuzzy_threshold:
                if try_select_checkbox(driver, row):
                    logger.info("    Selected single result (checkbox): '%s'", text)
                else:
                    driver.execute_script(
                        "arguments[0].scrollIntoView(true);", row
                    )
                    _safe_click(driver, row)
                    logger.info("    Selected single result (click): '%s'", text)
                save_screenshot(
                    driver,
                    f"client_{task_key}_{company[:30]}_clicked",
                )
                return True
            logger.info("    Single result '%s' below threshold (%.2f)", text, score)
            continue

        # Multiple candidates → use fuzzy matching to pick the best one
        if _select_best_match(driver, candidates, target_norm):
            save_screenshot(
                driver,
                f"client_{task_key}_{company[:30]}_clicked",
            )
            return True

    # Nothing matched
    logger.warning("  No confident match for company: %s", company)
    save_screenshot(driver, f"client_notfound_{task_key}_{company[:30]}")
    return False


def process_task_companies(
    driver: WebDriver,
    wait: WebDriverWait,
    task: dict,
    task_map: dict,
) -> None:
    """For a given task, open the Clientes tab, search and select each company.

    Always navigates back to the task list before returning so that the next
    loop iteration starts on the correct page.  The number of browser-back
    steps is tracked so we don't overshoot when no Clientes navigation occurred.
    """
    task_key = task.get("key", "unknown")
    companies = task_map.get(task_key, [])
    # Track how many pages deep we navigate beyond the task detail page.
    # search_and_click_task already put us 1 page deep (task detail).
    # open_clients_tab adds 1 more (Clientes sub-page).
    nav_depth = 1  # task detail page (always need at least 1 back)

    try:
        if not companies:
            logger.info("  No companies mapped for task '%s'.", task_key)
            return

        if not open_clients_tab(driver, wait):
            return

        nav_depth = 2  # Clientes tab was opened → need 2 backs

        clients_input = _find_search_input(driver, CLIENT_SEARCH_INPUT_SELECTORS)
        if not clients_input:
            logger.error("  Client search input not found.")
            return

        selected_any = False
        for company in companies:
            logger.info("  Searching company: %s", company)
            try:
                if search_and_select_company(
                    driver, clients_input, company, task_map, task_key
                ):
                    selected_any = True

                # Re-acquire the input (DOM may have changed)
                refreshed = _find_search_input(driver, CLIENT_SEARCH_INPUT_SELECTORS)
                if refreshed:
                    clients_input = refreshed
            except (StaleElementReferenceException, Exception) as exc:
                logger.warning("  Error searching company '%s': %r", company, exc)
                try:
                    _clear_and_type(driver, clients_input, "")
                except Exception:
                    pass
                time.sleep(CFG.ui_settle_delay)

        # Click "Adicionar" if at least one company was selected
        if selected_any:
            if find_and_click(wait, driver, ADD_BTN_SELECTORS):
                logger.info("  Clicked 'Adicionar' for task '%s'.", task_key)
                time.sleep(1.2)
            else:
                logger.warning("  'Adicionar' button not found for task '%s'.", task_key)
    finally:
        _go_back_to_task_list(driver, steps=nav_depth)


def _go_back_to_task_list(driver: WebDriver, steps: int = 2) -> None:
    """Navigate back to the task list (*steps* browser-back actions)."""
    for _ in range(steps):
        try:
            driver.back()
            WebDriverWait(driver, CFG.short_wait).until(
                EC.presence_of_all_elements_located(
                    (By.CSS_SELECTOR, "table tbody tr")
                )
            )
        except TimeoutException:
            time.sleep(CFG.ui_settle_delay)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_json(path: str, description: str) -> object:
    """Load and return JSON from *path*, logging on failure."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        logger.error("%s not found: %s", description, path)
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in %s: %r", path, exc)
    return None


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point: login → Processos → Gerenciar → iterate tasks."""
    driver = create_driver(headless=False)
    wait = WebDriverWait(driver, CFG.default_wait)

    try:
        perform_login(driver, wait)
        navigate_to_processos(driver, wait)
        try_expand_sidebar(driver, wait)
        navigate_to_gerenciar(driver, wait)

        # Load data files
        base_dir = Path(__file__).parent
        tasks_path = base_dir / "onvio_export" / "tasks.json"
        map_path = base_dir / "task_to_companies.json"

        tasks = load_json(str(tasks_path), "tasks.json") or []
        task_map = load_json(str(map_path), "task_to_companies.json") or {}
        if not isinstance(tasks, list):
            logger.error("tasks.json should be a JSON array.")
            tasks = []
        if not isinstance(task_map, dict):
            logger.error("task_to_companies.json should be a JSON object.")
            task_map = {}

        for task in tasks:
            label = task.get("label") or task.get("key")
            if not label:
                continue

            logger.info("Processing task: %s", label)
            if search_and_click_task(driver, wait, label):
                process_task_companies(driver, wait, task, task_map)
            else:
                logger.warning("Task '%s' not found after %d attempts.",
                               label, CFG.max_search_retries)

        time.sleep(5)
        save_screenshot(driver, "final_state")
        logger.info("Automation flow completed.")

    except Exception as exc:
        logger.exception("Fatal error during automation: %s", exc)
        save_screenshot(driver, "error_fatal")
        sys.exit(1)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
