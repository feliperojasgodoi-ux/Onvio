import json
import os
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
import difflib    
import re
import unicodedata
from collections import Counter




# Ruídos comuns em razão social (ajuste à sua base)
CORP_STOP = {
    "ltda","l.t.d.a","me","epp","sa","s.a","s/a","ei","eireli","ss","s.s",
    "matriz","filial","holding","holdings"
}
GENERIC_STOP = {
    "comercio","comércio","servicos","serviço","servicos.","serviço.",
    "industria","indústria","comercial","empresa","grupo",
    "e","&"
}
STOPWORDS = CORP_STOP | GENERIC_STOP

def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def tokenize(name: str):
    s = strip_accents(name).lower()
    # troca separadores por espaço
    s = re.sub(r"[^a-z0-9]+", " ", s)
    toks = [t for t in s.split() if t and t not in STOPWORDS]
    # remove duplicatas preservando ordem
    seen = set(); out = []
    for t in toks:
        if t not in seen:
            seen.add(t); out.append(t)
    return out

def build_query_basic(full_name: str, max_tokens: int = 3):
    """Gera 1 consulta curta tipo: 'arth cafe lanchonete'."""
    toks = tokenize(full_name)
    # regra simples: pega as primeiras palavras "fortes"
    core = [t for t in toks if not t.isdigit()]
    if not core:
        core = toks
    return " ".join(core[:max_tokens])

def build_query_candidates(full_name: str, max_tokens: int = 3):
    """
    Gera uma lista de consultas do mais específico para o mais curto.
    Ex.: ['arth cafe lanchonete', 'arth cafe', 'arth']
    """
    base = build_query_basic(full_name, max_tokens=max_tokens)
    toks = base.split()
    # gera do menor p/ maior e reverte para maior -> menor
    cands = [" ".join(toks[:k]) for k in range(1, len(toks)+1)]
    cands = list(dict.fromkeys(cands[::-1]))  # remove duplicatas preservando ordem (maior->menor)
    return cands  # do menor p/ maior

# (Opcional) Versão que usa frequência da sua lista de empresas para preferir palavras raras
def build_query_smart(full_name: str, all_company_names, max_tokens: int = 3):
    """
    Calcula frequência de tokens na base e escolhe os mais raros do nome.
    -> Consulta tende a ser mais discriminativa.
    """
    # índice de frequências
    def all_tokens(names):
        for n in names:
            for t in tokenize(n):
                yield t
    freq = Counter(all_tokens(all_company_names))

    toks = tokenize(full_name)
    toks_sorted = sorted(toks, key=lambda t: (freq.get(t, 0), len(t)), reverse=False)
    core = [t for t in toks_sorted if not t.isdigit()]
    if not core:
        core = toks_sorted
    return " ".join(core[:max_tokens])


def _find_and_click(wait, driver, selectors):
    for sel in selectors:
        try:
            el = wait.until(EC.element_to_be_clickable(sel))
            driver.execute_script("arguments[0].scrollIntoView(true);", el)
            try:
                el.click()
            except Exception:
                driver.execute_script("arguments[0].click();", el)
            return True
        except Exception:
            continue
    return False

def _find_input_and_type(wait, driver, candidates, value):
    for cand in candidates:
        try:
            el = wait.until(EC.element_to_be_clickable(cand))
            el.clear()
            el.send_keys(value)
            return True
        except Exception:
            continue
    return False

def try_slide_sidebar(driver, wait, x_offset=200):
    """Tenta arrastar a borda/handle da sidebar para a direita; fallback para JS."""
    candidates = [
        (By.CSS_SELECTOR, "div[class*='c-ikMfhs']"),
        (By.CSS_SELECTOR, "div[class*='app-sidebar']"),
        (By.CSS_SELECTOR, "div[role='navigation']"),
        (By.XPATH, "//div[contains(@class,'sidebar') or contains(@class,'menu')][1]"),
    ]
    for sel in candidates:
        try:
            el = wait.until(EC.presence_of_element_located(sel))
            ActionChains(driver).move_to_element(el).click_and_hold(el).move_by_offset(x_offset, 0).release().perform()
            time.sleep(0.5)
            return True
        except Exception:
            continue
    # fallback: tentar remover classe collapsed / forçar estilo via JS
    try:
        driver.execute_script("""
            var el = document.querySelector('div[class*=\"sidebar\"], div[role=\"navigation\"]');
            if (el) { el.classList.remove('collapsed'); el.style.transform='translateX(0px)'; }
        """)
        time.sleep(0.5)
        return True
    except Exception:
        return False

def main():
    url = "https://onvio.com.br/login/#/"   # ajuste se necessário

    username = os.environ.get("WORK_USER", "felipe.godoi@ordec.com.br")
    password = os.environ.get("WORK_PASS", "Tonto2402")

    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    # options.add_argument("--headless")  # descomente apenas quando estiver tudo funcionando

    chromedriver_path = os.path.join(os.path.dirname(__file__), "chromedriver.exe")
    service = Service(chromedriver_path)
    driver = webdriver.Chrome(service=service, options=options)
    wait = WebDriverWait(driver, 20)

    try:
        driver.get(url)

        # 1) botão inicial "Entrar"/continuar (vários possíveis seletores)
        initial_btn_selectors = [
            (By.ID, "trauth-continue-signin-bt"),
            (By.ID, "trauth-continue-signin-btn"),
            (By.NAME, "action"),
            (By.XPATH, "//button[contains(., 'Entrar')]"),
            (By.CSS_SELECTOR, "button[type='submit']"),
        ]
        _find_and_click(wait, driver, initial_btn_selectors)

        # 2) preencher e-mail (tenta múltiplos nomes/ids comuns)
        email_candidates = [
            (By.NAME, "username"),
            (By.NAME, "email"),
            (By.ID, "username"),
            (By.CSS_SELECTOR, "input[type='email']"),
            (By.CSS_SELECTOR, "input[type='text']"),
        ]
        filled = _find_input_and_type(wait, driver, email_candidates, username)
        if not filled:
            raise RuntimeError("Campo de e-mail não encontrado. Ajuste os seletores.")

        # 3) clicar no botão após e-mail (se houver)
        _find_and_click(wait, driver, initial_btn_selectors)

        # 4) preencher senha (após avançar)
        password_candidates = [
            (By.NAME, "password"),
            (By.ID, "password"),
            (By.CSS_SELECTOR, "input[type='password']"),
        ]
        filled_pw = _find_input_and_type(wait, driver, password_candidates, password)
        if not filled_pw:
            raise RuntimeError("Campo de senha não encontrado. Ajuste os seletores.")

        # 5) clicar no botão final de envio
        final_btn_selectors = [
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.XPATH, "//button[contains(., 'Entrar') or contains(., 'Login')]"),
            (By.NAME, "action"),
        ]
        clicked = _find_and_click(wait, driver, final_btn_selectors)
        if not clicked:
            raise RuntimeError("Botão final de login não encontrado/clicável. Ajuste seletores.")

        # 6) clica no menu de seleção
        menu_btn_selectors = [
            (By.CSS_SELECTOR, "button[aria-label='Menu']"),
            (By.ID, "bm-header-app-menu-toggle"),
        ]   
        clicked = _find_and_click(wait, driver, menu_btn_selectors)
        if not clicked:
            raise RuntimeError("Botão final de login não encontrado/clicável. Ajuste seletores.")

        # 7) clica na opção "Processos" (abre nova aba)
        processos_btn_selectors = [
            (By.XPATH, "//a[contains(., 'Processos')]"),
            (By.LINK_TEXT, "Processos"),
        ]

        # guarda handles antes do clique
        handles_before = driver.window_handles.copy()
        clicked = _find_and_click(wait, driver, processos_btn_selectors)
        if not clicked:
            raise RuntimeError("Botão 'Processos' não encontrado/clicável. Ajuste seletores.")

        # espera nova aba e troca para ela
        try:
            WebDriverWait(driver, 10).until(lambda d: len(d.window_handles) > len(handles_before))
            new_handles = [h for h in driver.window_handles if h not in handles_before]
            if new_handles:
                driver.switch_to.window(new_handles[0])
            else:
                # como fallback, troca para a última aba
                driver.switch_to.window(driver.window_handles[-1])
        except Exception:
            # fallback: seleciona última aba mesmo se timeout
            driver.switch_to.window(driver.window_handles[-1])
        
        # tenta deslizar/expandir a sidebar antes de acessar itens nela
        try_slide_sidebar(driver, wait, x_offset=220)

        # 8) clica na opção "Configure" (usar data-qe-id se disponível)
        try:
            configure_el = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "div[data-qe-id='gestta_menu-configure']")))
            driver.execute_script("arguments[0].scrollIntoView(true);", configure_el)
            try:
                configure_el.click()
            except Exception:
                driver.execute_script("arguments[0].click();", configure_el)
        except Exception:
            # fallback por texto caso o data-qe-id não exista
            if not _find_and_click(wait, driver, [(By.XPATH, "//div[normalize-space()='Configure']"), (By.PARTIAL_LINK_TEXT, "Config")]):
                raise RuntimeError("Botão 'Configure' não encontrado/clicável. Ajuste seletores.")

        # aguarda menu/submenu abrir
        time.sleep(0.5)

        # 9) clica em "Gerenciar" — XPath robusto + id como fallback
        gerenciar_xpath_candidates = [
            "//a[@id='gestta_menu-tarefas-recorrentes-gerenciar']",
            "//a[normalize-space()='Gerenciar']",
            "//div[normalize-space()='Gerenciar']",
            "//button[normalize-space()='Gerenciar']",
            "//a[contains(., 'Gerenciar')]",
        ]
        gerenciou = False
        for xp in gerenciar_xpath_candidates:
            try:
                gerenciar_el = wait.until(EC.element_to_be_clickable((By.XPATH, xp)))
                driver.execute_script("arguments[0].scrollIntoView(true);", gerenciar_el)
                try:
                    gerenciar_el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", gerenciar_el)
                gerenciou = True
                break
            except Exception:
                continue

        if not gerenciou:
            raise RuntimeError("Botão 'Gerenciar' não encontrado/clicável. Ajuste seletores.")

        # --- novo: ler tasks.json e buscar cada label na caixa de pesquisa ---
        tasks_path = os.path.join(os.path.dirname(__file__), "onvio_export", "tasks.json")
        map_file = os.path.join(os.path.dirname(__file__), "..", "task_to_companies.json")

        try:
            with open(tasks_path, "r", encoding="utf-8") as f:
                tasks = json.load(f)
        except Exception as e:
            print("Não foi possível abrir tasks.json:", e)
            tasks = []
        
        try:
            with open(map_file, "r", encoding="utf-8") as f:
                task_map = json.load(f)
        except Exception as e:
            print("Erro ao ler task_to_companies.json:", e)
            task_map = {}

        search_input_selectors = [
            (By.CSS_SELECTOR, "input[placeholder*='Pesquisar por nome']"),
            (By.CSS_SELECTOR, "input[placeholder*='Pesquisar']"),
            (By.CSS_SELECTOR, "input.form-control"),
            (By.CSS_SELECTOR, "input[type='search']"),
        ]

        for task in tasks:
            label = task.get("label") or task.get("key")
            if not label:
                continue
            print("Buscando:", label)
            # tenta até 3 vezes por item
            found = False
            for attempt in range(3):
                # localizar campo de busca
                input_el = None
                for sel in search_input_selectors:
                    try:
                        input_el = wait.until(EC.element_to_be_clickable(sel))
                        break
                    except Exception:
                        continue
                if not input_el:
                    print("Campo de busca não encontrado. Abortando busca deste item.")
                    break

                # limpar e digitar
                try:
                    input_el.clear()
                except Exception:
                    driver.execute_script("arguments[0].value = '';", input_el)
                input_el.send_keys(label)
                time.sleep(0.4)
                # enviar enter para disparar busca
                try:
                    input_el.send_keys(Keys.ENTER)
                except Exception:
                    pass

                # aguardar resultados (tabela de resultados ou outro indicador)
                try:
                    time.sleep(0.6)  # pequena espera para a UI atualizar

                    # 1) busca por links com texto exatamente igual ao label
                    exact_xpath = f"//a[normalize-space()={json.dumps(label)}]"
                    links = driver.find_elements(By.XPATH, exact_xpath)
                    print(f"Debug: exact links found={len(links)} for label='{label}'")

                    # 2) fallback case-insensitive (se nada foi encontrado)
                    if not links:
                        ci_xpath = f"//a[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz') = {json.dumps(label.lower())}]"
                        links = driver.find_elements(By.XPATH, ci_xpath)
                        print(f"Debug: case-insensitive links found={len(links)}")

                    # 3) se encontrou, clica no primeiro
                    if links:
                        link = links[0]
                        print("Debug: clicking link text:", link.text, " href:", link.get_attribute("href"))
                        driver.execute_script("arguments[0].scrollIntoView(true);", link)
                        pre_url = driver.current_url
                        try:
                            link.click()
                        except Exception as e:
                            print("Debug: click() failed, using JS click:", repr(e))
                            driver.execute_script("arguments[0].click();", link)

                        # aguarda mudança de URL ou (se a app não muda URL) aguarda elemento de detalhe (ajuste se necessário)
                        try:
                            WebDriverWait(driver, 10).until(lambda d: d.current_url != pre_url)
                        except Exception:
                            time.sleep(1)
                        print(f"Encontrado e clicado '{label}'.")
                        # tenta clicar em "Clientes" na nova tela
                        clientes_btn_selectors = [
                            (By.XPATH, "//a[contains(@class,'btn tr-btn-group ng-scope') and contains(@href,'/customer')]"),
                            (By.LINK_TEXT, "Clientes")
                        ]
                        # guarda handles antes do clique
                        handles_before = driver.window_handles.copy()
                        clicked = _find_and_click(wait, driver, clientes_btn_selectors)
                        time.sleep(10)
                        if not clicked:
                            print("Botão 'Clientes' não encontrado/clicável. Ajuste seletores.")
                        
                        # --- abrir a aba "Clientes" e usar a barra de pesquisa específica dela ---
                        # clique no botão "Clientes" (mesmo local que você tinha)
                        clientes_btn_selectors = [
                            (By.XPATH, "//a[contains(., 'Clientes')]"),
                            (By.LINK_TEXT, "Clientes")
                        ]
                        clicked = _find_and_click(wait, driver, clientes_btn_selectors)
                        if not clicked:
                            print("Botão 'Clientes' não encontrado/clicável. Ajuste seletores.")
                        else:
                            # aguarda a área de clientes aparecer e o input específico
                            def _find_clients_search_input(driver, wait):
                                candidates = [
                                    (By.CSS_SELECTOR, "input[ng-model*='foreignListFilter']"),
                                    (By.CSS_SELECTOR, "input[ng-model*='foreignCtrl.foreignListFilter']"),
                                    (By.CSS_SELECTOR, "input[placeholder*='Pesquisar por nome']"),
                                    (By.CSS_SELECTOR, "input[placeholder*='Pesquisar']"),
                                    (By.CSS_SELECTOR, "input.form-control"),
                                    (By.CSS_SELECTOR, "input[type='search']"),
                                ]
                                for sel in candidates:
                                    try:
                                        el = WebDriverWait(driver, 8).until(EC.element_to_be_clickable(sel))
                                        return el
                                    except Exception:
                                        continue
                                return None

                            # espera até o campo de clientes aparecer
                            clients_input = _find_clients_search_input(driver, wait)
                            if not clients_input:
                                print("Campo de pesquisa de Clientes não encontrado. Verifique selectors e tempo de espera.")
                            else:
                                # pega lista de empresas do mapa (task_map deve existir no escopo)
                                companies = task_map.get(task.get("key"), []) if isinstance(task_map, dict) else []
                                if not companies:
                                    print(f"  Nenhuma empresa mapeada para task {task.get('key')}")
                                for company in companies:
                                    try:
                                        print(f"  Pesquisando empresa: {company}")

                                        # gera consultas candidatas (curtas -> longas) e uma consulta "smart"
                                        all_companies = [c for cl in task_map.values() for c in cl] if isinstance(task_map, dict) else []
                                        q_cands = build_query_candidates(company, max_tokens=3)
                                        smart = build_query_smart(company, all_companies, max_tokens=3) if all_companies else None
                                        if smart and smart not in q_cands:
                                            q_cands.append(smart)

                                        clicked_flag = False
                                        selected_any = False
                                        target_norm = strip_accents(company).lower()

                                        for q in q_cands:
                                            # digita a consulta na caixa específica de Clientes
                                            try:
                                                clients_input.clear()
                                            except Exception:
                                                driver.execute_script("arguments[0].value = '';", clients_input)
                                            clients_input.send_keys(q)
                                            time.sleep(0.6)
                                            try:
                                                clients_input.send_keys(Keys.ENTER)
                                            except Exception:
                                                pass
                                            time.sleep(0.6)

                                            # coleta linhas/itens visíveis nos resultados (ajuste seletor se necessário)
                                            results = driver.find_elements(By.XPATH, "//table//tbody//tr | //ul//li | //div[contains(@class,'foreign-list-results')]//div[contains(@class,'row') or contains(@class,'item')] | //div[@class='ibox-content']//div[contains(@class,'row')]")
                                            candidates = []
                                            for r in results[:10]:
                                                text = r.text.strip()
                                                # tenta pegar anchor se existir (para referência de texto)
                                                try:
                                                    a = r.find_element(By.TAG_NAME, "a")
                                                    text = a.text.strip() or text
                                                except Exception:
                                                    pass
                                                if not text:
                                                    continue
                                                candidates.append((r, text))

                                            if not candidates:
                                                # sem resultados visíveis para essa query
                                                print(f"    query '{q}' -> 0 resultados")
                                                continue

                                            # Se houver mais de um resultado visível, clicar/selecionar o primeiro (comportamento solicitado)
                                            first_row, first_txt = candidates[0]
                                            try:
                                                # tenta selecionar checkbox dentro da linha (preferencial)
                                                checkbox = None
                                                try:
                                                    checkbox = first_row.find_element(By.CSS_SELECTOR, "input[type='checkbox']")
                                                except Exception:
                                                    # outro possível seletor para checkbox
                                                    try:
                                                        checkbox = first_row.find_element(By.CSS_SELECTOR, ".all-check-box input[type='checkbox']")
                                                    except Exception:
                                                        checkbox = None

                                                if checkbox:
                                                    driver.execute_script("arguments[0].scrollIntoView(true);", checkbox)
                                                    try:
                                                        if not checkbox.is_selected():
                                                            checkbox.click()
                                                    except Exception as e:
                                                        print("    checkbox click() falhou, usando JS click:", repr(e))
                                                        driver.execute_script("arguments[0].click();", checkbox)
                                                    print(f"    query '{q}' -> múltiplos/primeiro resultado selecionado: '{first_txt}'")
                                                else:
                                                    # se não houver checkbox, clicar na linha/anchor como fallback
                                                    driver.execute_script("arguments[0].scrollIntoView(true);", first_row)
                                                    try:
                                                        first_row.click()
                                                    except Exception as e:
                                                        print("    click() falhou no primeiro resultado, usando JS click:", repr(e))
                                                        driver.execute_script("arguments[0].click();", first_row)
                                                    

                                                
                                                clicked_flag = True
                                                selected_any = True
                                                break  # saiu do loop de queries para esta company
                                            except Exception as e:
                                                print("    Erro ao selecionar primeiro resultado:", repr(e))
                                                # segue para lógica fuzzy se necessário

                                            # caso haja apenas um candidato, tratamos com fuzzy/confirmação
                                            if len(candidates) == 1:
                                                el, txt = candidates[0]
                                                norm = strip_accents(txt).lower()
                                                score = difflib.SequenceMatcher(None, target_norm, norm).ratio()
                                                print(f"    única linha -> '{txt}' score={score:.2f}")
                                                if score >= 0.0:  # sempre aceitar única linha (ajuste limiar se quiser)
                                                    # tenta selecionar checkbox primeiro
                                                    try:
                                                        checkbox = el.find_element(By.CSS_SELECTOR, "input[type='checkbox']")
                                                    except Exception:
                                                        checkbox = None
                                                    if checkbox:
                                                        try:
                                                            if not checkbox.is_selected():
                                                                checkbox.click()
                                                        except Exception:
                                                            driver.execute_script("arguments[0].click();", checkbox)
                                                        print(f"    única linha selecionada por checkbox: '{txt}'")
                                                        selected_any = True
                                                        clicked_flag = True
                                                        
                                                        break
                                                    else:
                                                        try:
                                                            el.click()
                                                        except Exception:
                                                            driver.execute_script("arguments[0].click();", el)
                                                        print(f"    única linha clicada (fallback): '{txt}'")
                                                        clicked_flag = True
                                                        
                                                        break

                                            # se não saiu, tenta fuzzy (apenas quando houver 1 candidato ou nenhum clique anterior)
                                            best = None
                                            best_score = 0.0
                                            for el, txt in candidates:
                                                norm = strip_accents(txt).lower()
                                                score = difflib.SequenceMatcher(None, target_norm, norm).ratio()
                                                if score > best_score:
                                                    best_score = score
                                                    best = (el, txt, score)

                                            print(f"    query '{q}' -> candidatos={len(candidates)} best='{best[1] if best else ''}' score={best_score:.2f}")

                                            # limiar ajustável (0.65-0.75 recomendado)
                                            if best and best_score >= 0.65:
                                                el_to_click = best[0]
                                                # tenta selecionar checkbox se existir
                                                try:
                                                    checkbox = el_to_click.find_element(By.CSS_SELECTOR, "input[type='checkbox']")
                                                except Exception:
                                                    checkbox = None
                                                if checkbox:
                                                    try:
                                                        if not checkbox.is_selected():
                                                            checkbox.click()
                                                    except Exception:
                                                        driver.execute_script("arguments[0].click();", checkbox)
                                                    
                                                else:
                                                    try:
                                                        driver.execute_script("arguments[0].scrollIntoView(true);", el_to_click)
                                                        el_to_click.click()
                                                    except Exception as e:
                                                        print("    click() falhou, usando JS click:", repr(e))
                                                        driver.execute_script("arguments[0].click();", el_to_click)
                                                   

                                                driver.save_screenshot(f"client_{task.get('key')}_{company[:30].replace('/', '_')}_clicked.png")
                                                clicked_flag = True
                                                selected_any = True
                                                break  # saiu do loop de queries para esta company

                                        if not clicked_flag:
                                            print(f"  -> não foi encontrado resultado confiável para: {company}")
                                            driver.save_screenshot(f"client_notfound_{task.get('key')}_{company[:30].replace('/', '_')}.png")

                                        # re-obter o input para próxima iteração (algumas páginas reposicionam o DOM)
                                        try:
                                            clients_input = _find_clients_search_input(driver, wait)
                                        except Exception:
                                            pass

                                    except Exception as e:
                                        print("  Erro ao pesquisar/clicar empresa:", e)
                                        try:
                                            clients_input.clear()
                                        except Exception:
                                            driver.execute_script("arguments[0].value = '';", clients_input)
                                        time.sleep(0.4)

                                # fim do loop companies - após todas as empresas tentadas, clicar em "Adicionar" se tivermos selecionado algo
                                if selected_any:
                                    add_selectors = [
                                        (By.CSS_SELECTOR, "button.btn.btn-block.btn-success"),
                                        (By.XPATH, "//button[normalize-space()='Adicionar']"),
                                        (By.XPATH, "//button[contains(., 'Adicionar')]")
                                    ]
                                    added = _find_and_click(wait, driver, add_selectors)
                                    if added:
                                        print(f"  -> clicado 'Adicionar' para task {task.get('key')}")
                                        # esperar pequeno tempo para ação completar
                                        time.sleep(1.2)
                                        # voltar 2 páginas para retornar à lista de tarefas
                                        try:
                                            driver.back()
                                            WebDriverWait(driver, 8).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "table tbody tr")))
                                        except Exception:
                                            time.sleep(0.5)
                                        try:
                                            driver.back()
                                            WebDriverWait(driver, 8).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "table tbody tr")))
                                        except Exception:
                                            time.sleep(0.5)
                                    else:
                
                                      print("  -> botão 'Adicionar' não encontrado/clicável. Verifique seletores.")
                except Exception as e:
                                    print("Erro ao clicar em 'Adicionar':", e)
            if not found:
                print(f"Não foi possível encontrar ou clicar na task '{label}' após várias tentativas.")
                
        time.sleep(30)
        driver.save_screenshot("screenshot_after_login.png")
        print("Fluxo executado")

    except Exception as e:
        print("Erro durante a automação:", e)
        try:
            driver.save_screenshot("error_screenshot.png")
        except Exception:
            pass
    finally:
        driver.quit()

if __name__ == "__main__":
    main()



