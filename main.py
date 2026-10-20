import argparse
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from playwright.sync_api import sync_playwright, Page

load_dotenv(Path(__file__).parent / ".env")

ERP_URL      = os.getenv("INNOVARO_URL", "http://192.168.3.140/sistema")
USERNAME     = os.getenv("INNOVARO_USERNAME", "ti_admin")
PASSWORD     = os.getenv("INNOVARO_PASSWORD", "")
HEADLESS     = os.getenv("INNOVARO_HEADLESS", "false").lower() == "true"
SLOW_MO      = int(os.getenv("INNOVARO_SLOW_MO", "250"))
CLICK_DELAY  = int(os.getenv("INNOVARO_CLICK_DELAY", "800"))   # ms entre cliques de navegação

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


def _log(msg: str):
    print(f"[conferencia_pedido] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Tratamento de dialogs de sessão
# O Innovaro exibe até 3 tipos de dialog que bloqueiam a navegação:
#   1. "A sua sessão será encerrada em breve" → clicar Não (manter sessão)
#   2. "Erro 500 / Script Aborted"            → clicar Fechar
#   3. "A sua sessão expirou"                 → clicar Fechar (volta ao login)
# ---------------------------------------------------------------------------
def fechar_dialogs(page: Page) -> bool:
    """
    Fecha todos os dialogs de sessão visíveis.
    Retorna True se algum dialog foi fechado (pode indicar necessidade de re-login).
    """
    fechou_algum = False

    for _ in range(5):
        achou = False

        # Dialog 1: sessão será encerrada — clicar Não para manter
        try:
            nao = page.get_by_role("button", name="Não", exact=True).first
            if nao.is_visible(timeout=800):
                _log("Dialog 'sessão será encerrada' detectado → clicando Não")
                nao.click()
                page.wait_for_timeout(600)
                achou = True
                fechou_algum = True
        except Exception:
            pass

        # Dialog 2 e 3: botão "Fechar" dentro de alertdialog/dialog (NÃO o X da aba)
        try:
            for fechar_btn in page.locator("[role='alertdialog'] button, [role='dialog'] button").all():
                try:
                    txt = fechar_btn.inner_text(timeout=300).strip()
                    if txt == "Fechar" and fechar_btn.is_visible(timeout=500):
                        _log("Dialog com 'Fechar' detectado → fechando")
                        fechar_btn.click()
                        page.wait_for_timeout(600)
                        achou = True
                        fechou_algum = True
                        break
                except Exception:
                    pass
        except Exception:
            pass

        if not achou:
            break

    return fechou_algum


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def login(page: Page):
    _log(f"Acessando {ERP_URL}")
    page.goto(ERP_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    fechar_dialogs(page)

    # Se já estiver logado (botão Menu visível), não faz nada
    if page.get_by_role("button", name="Menu", exact=True).count():
        _log("Sessão já ativa, login ignorado.")
        return

    _log("Preenchendo credenciais")
    page.get_by_role("textbox", name="Usuário").fill(USERNAME)
    page.get_by_role("textbox", name="Senha").fill(PASSWORD)
    page.get_by_role("button", name="Entrar", exact=True).click()

    _log("Aguardando tela principal")
    for _ in range(25):
        fechar_dialogs(page)
        if page.get_by_role("button", name="Menu", exact=True).count():
            page.wait_for_timeout(1000)
            _log("Login realizado com sucesso.")
            return
        page.wait_for_timeout(1000)

    page.screenshot(path=str(OUTPUT_DIR / "erro_login.png"), full_page=True)
    raise RuntimeError("Login não chegou na tela principal. Veja erro_login.png.")


# ---------------------------------------------------------------------------
# Menu principal
# ---------------------------------------------------------------------------
def abrir_menu_principal(page: Page):
    """Abre o menu lateral. Idempotente — não faz nada se já estiver aberto."""
    fechar_menu = page.get_by_role("button", name="Fechar menu", exact=True)
    try:
        if fechar_menu.is_visible(timeout=800):
            return
    except Exception:
        pass

    page.get_by_role("button", name="Menu", exact=True).click()
    fechar_menu.wait_for(timeout=15000)
    page.wait_for_timeout(500)


# ---------------------------------------------------------------------------
# Navegação: Venda > Consultas > Relatório
# ---------------------------------------------------------------------------
NOME_RELATORIO = "A - Relatorio de pendencia para Importar na Planilha - Ti_Admin"
NOME_ABA       = "A - Relatorio de pendencia para Importar na Planilha Fechar"

def navegar_relatorio_pendencia(page: Page):
    _log("Abrindo menu principal")
    abrir_menu_principal(page)
    page.wait_for_timeout(CLICK_DELAY)

    _log("Clicando em Venda")
    page.get_by_text("Venda", exact=True).click()
    page.wait_for_timeout(CLICK_DELAY)

    _log("Clicando em Consultas")
    page.get_by_role("menu").get_by_text("Consultas", exact=True).click()
    page.wait_for_timeout(CLICK_DELAY)

    _log(f"Clicando em '{NOME_RELATORIO}'")
    page.get_by_text(NOME_RELATORIO, exact=True).click()
    page.wait_for_timeout(CLICK_DELAY)

    # Aguardar a aba do relatório abrir
    _log("Aguardando aba do relatório abrir")
    page.get_by_role("tab", name=NOME_ABA).wait_for(timeout=30000)
    page.wait_for_timeout(2000)

    # Fechar qualquer dialog que apareça após abrir
    fechar_dialogs(page)

    # Clicar na aba para ativar currentProcess (necessário para acessar o grid)
    page.get_by_role("tab", name=NOME_ABA).click(timeout=5000)
    page.wait_for_timeout(1500)

    _log("Tela do relatório aberta com sucesso.")


# ---------------------------------------------------------------------------
# Preenchimento do formulário de variáveis (wf-grid virtual rendering)
# Os campos não são elementos HTML — são manipulados via grid.updateCellValue()
# da API JavaScript do Innovaro (window.Environment).
# ---------------------------------------------------------------------------

# Nomes dos campos (prefixo dsvfilter_PED_)
CAMPO_APROVACAO      = "dsvfilter_PED_XAPROVACAO"
CAMPO_CLASSIFICACAO  = "dsvfilter_PED_CLASSE"
CAMPO_CLASSE_RECURSO = "dsvfilter_PED_RECURSO.CLASSE"
CAMPO_RECURSO        = "dsvfilter_PED_RECURSO"
CAMPO_INICIO         = "dsvfilter_PED_EMISSAOMOV_START"
CAMPO_FIM            = "dsvfilter_PED_EMISSAOMOV_END"
CAMPO_BAIXA          = "dsvfilter_PED_XBAIXA"
CAMPO_CANCELAMENTO   = "dsvfilter_PED_XCANCELAMENTOSALDO"


def _setar_campo_grid(page: Page, nome_campo: str, valor: str):
    """Define o valor de um campo combo do wf-grid 'vars' via updateCellValue."""
    page.evaluate(
        """([nome, val]) => {
            const env = window.Environment?.getInstance?.();
            const grid = env?.currentProcess?.getGrid?.('vars');
            if (!grid) throw new Error('grid vars nao encontrado');
            grid.updateCellValue({
                gridName: 'vars',
                fieldName: nome,
                value: val,
                displayValue: val
            });
        }""",
        [nome_campo, valor],
    )
    page.wait_for_timeout(300)


def _digitar_no_grid(page: Page, valor: str, confirmar_tab: bool = True):
    """
    Digita um valor na célula atualmente focada do wf-grid e confirma com Tab.
    O grid deve já ter o foco do browser antes de chamar esta função.
    Para limpar: passar valor=''.
    """
    page.keyboard.press("Control+A")
    if valor:
        page.keyboard.type(valor, delay=60)
    else:
        page.keyboard.press("Delete")
    page.wait_for_timeout(350)
    if confirmar_tab:
        page.keyboard.press("Tab")
        page.wait_for_timeout(500)


def preencher_variaveis(page: Page, data_inicio: str, data_fim: str | None = None):
    """
    Conforme boas práticas (seção 19):
    - Combos (1, 11, 12): updateCellValue — sem Tab.
    - Campos memo (2,3,4): Tab + Ctrl+A + type + Tab.
    - Campos date (5,6): type("h") + Tab.
    - Campos date opcionais (7,8,9,10): Tab para percorrer (vazios).

    Tab order: 2→Classificação 3→ClasseRecurso 4→Recurso 5→Início
               6→Fim 7→PrevEmissãoDocInício 8→PrevEmissãoDocFim
               9→ProgEntregaInício 10→ProgEntregaFim

    data_inicio / data_fim: dd/mm/yyyy. Se data_fim for None, usa o atalho 'h' (hoje).
    """
    _log("Preenchendo variáveis do relatório")

    # Aprovação via updateCellValue (combo — cursor fica em Aprovação após isso)
    _setar_campo_grid(page, CAMPO_APROVACAO, "Aprovados")
    # Baixa e Cancelamento já têm defaults corretos ("Sem Baixa" / "Saldo Não Cancelado").
    # NÃO setar via updateCellValue aqui: mudaria o cursor interno do grid para o
    # campo 11/12, e o Tab subsequente partiria do lugar errado.

    # Focar o grid com cursor em Aprovação (campo 1)
    page.evaluate("""() => {
        const env = window.Environment?.getInstance?.();
        const grid = env?.currentProcess?.getGrid?.('vars');
        if (!grid) throw new Error('grid vars nao encontrado');
        grid.focus();
    }""")
    page.wait_for_timeout(600)

    # Usar o iframe como alvo explícito para que Tab e type cheguem ao wf-grid
    # e não a botões da toolbar da página principal.
    fr = page.locator("iframe").last

    # Campo 2: Classificação (memo)
    fr.press("Tab")
    page.wait_for_timeout(400)
    fr.press_sequentially("Vendas", delay=60)
    fr.press("Tab")
    page.wait_for_timeout(500)

    # Campo 3: Classe Recurso (memo)
    fr.press_sequentially("Implementos", delay=60)
    fr.press("Tab")
    page.wait_for_timeout(500)

    # Campo 4: Recurso (memo, limpar)
    fr.press("Delete")
    fr.press("Tab")
    page.wait_for_timeout(500)

    # Campo 5: Início (date)
    _log(f"Data início: {data_inicio}")
    fr.press_sequentially(data_inicio, delay=60)
    fr.press("Tab")
    page.wait_for_timeout(500)

    # Campo 6: Fim (date) — 'h' é o atalho do ERP para hoje; se vier data explícita, digita por extenso
    dt_fim = data_fim
    if dt_fim:
        _log(f"Data fim: {dt_fim}")
        fr.press_sequentially(dt_fim, delay=60)
    else:
        _log("Data fim: hoje (atalho 'h')")
        fr.press_sequentially("h", delay=60)
    fr.press("Tab")
    page.wait_for_timeout(500)

    # Campo 7: Previsão Emissão Documento Início (date, vazio)
    fr.press("Tab")
    page.wait_for_timeout(400)

    # Campo 8: Previsão Emissão Documento Fim (date, vazio)
    fr.press("Tab")
    page.wait_for_timeout(400)

    # Campo 9: Prog Entrega Início (date, vazio)
    fr.press("Tab")
    page.wait_for_timeout(400)

    # Campo 10: Prog Entrega Fim (date, vazio) — último campo via Tab

    _log("Variáveis preenchidas.")


# ---------------------------------------------------------------------------
# Aguardar o ERP processar
# ---------------------------------------------------------------------------
def _relatorio_carregado(page: Page) -> bool:
    """
    Retorna True quando o relatório terminou de renderizar no iframe.
    O relatório está pronto quando:
      - Não há overlay de progresso
      - O iframe tem conteúdo HTML real (>50 elementos ou tem tabelas)
    """
    try:
        frame_handle = page.locator("iframe").last.element_handle(timeout=500)
        if not frame_handle:
            return False
        frame = frame_handle.content_frame()
        if not frame:
            return False

        return frame.evaluate("""() => {
            // Checar se ainda está carregando
            const body = document.body?.innerText || "";
            const busy = /Carregando|Processando|Gerando|Aguarde/i.test(body)
                || !!document.querySelector(".wf-progress-dialog__content, #progressMessageBox, #statusMessageBox")
                || !!document.querySelector("[aria-busy='true']");
            if (busy) return false;

            // Checar se o relatório HTML já está renderizado
            const elements = document.querySelectorAll('*').length;
            const tables = document.querySelectorAll('table').length;
            // Relatório carregado tem muito mais do que o scaffolding vazio (17 elementos)
            return elements > 50 || tables > 0;
        }""")
    except Exception:
        return False   # qualquer erro → considerar não carregado


def aguardar_relatorio_carregar(page: Page, timeout_ms: int = 120_000):
    """Aguarda o relatório terminar de carregar após clicar em Visualizar."""
    _log("Aguardando relatório carregar...")
    page.wait_for_timeout(2000)   # dar tempo para o ERP iniciar o carregamento

    inicio = time.time()
    while (time.time() - inicio) * 1000 < timeout_ms:
        if _relatorio_carregado(page):
            page.wait_for_timeout(1000)   # pausa extra para renderização final
            _log("Relatório carregado.")
            return
        page.wait_for_timeout(800)

    raise TimeoutError("Relatório não carregou dentro do tempo esperado.")


# ---------------------------------------------------------------------------
# Ler dados do relatório diretamente do grid via JavaScript
# ---------------------------------------------------------------------------
def ler_dados_relatorio(page: Page) -> pd.DataFrame:
    """
    Lê os dados diretamente do wf-grid do relatório após o Visualizar,
    sem passar pela tela de exportação do Innovaro.
    Tenta frame por frame até encontrar o grid com dados.
    """
    _log("Lendo dados do relatório...")

    JS_LER_GRID = """() => {
        try {
            const env = window.Environment?.getInstance?.();
            const proc = env?.currentProcess;
            if (!proc) return { erro: 'currentProcess null', procName: null };

            // Varrer todos os grids do processo para achar o que tem dados
            let grid = null;
            let gridName = null;
            const gridsObj = proc.grids || proc._grids || {};
            for (const [nm, g] of Object.entries(gridsObj)) {
                try {
                    if (g?.recordCount > 0) { grid = g; gridName = nm; break; }
                } catch(e) {}
            }

            if (!grid) return { erro: 'nenhum grid com dados', procName: proc.name };

            // Coletar headers via linked list
            const headers = [];
            try {
                const allF = grid.fields || grid._fields || {};
                let cur = Object.values(allF)[0];
                if (cur) {
                    while (cur.priorField) cur = cur.priorField;
                    while (cur) {
                        headers.push({ name: cur.name, label: cur.label || cur.tableViewLabel || cur.name });
                        cur = cur.nextField;
                    }
                }
            } catch(e) {}

            // Coletar dados
            const rows = [];
            const total = grid.recordCount || 0;
            const savedRec = grid.recNo;
            for (let i = 1; i <= total; i++) {
                try {
                    grid.gotoRecord(i);
                    const row = {};
                    for (const h of headers) {
                        try {
                            const f = grid.field(h.name);
                            row[h.label] = f?.getDisplayValue?.() ?? f?.getValue?.() ?? f?.displayValue ?? '';
                        } catch(e) { row[h.label] = ''; }
                    }
                    rows.push(row);
                } catch(e) {}
            }
            try { grid.gotoRecord(savedRec); } catch(e) {}

            return { gridName, recordCount: total, headers: headers.map(h => h.label), rows };
        } catch(e) {
            return { erro: e?.message || String(e) };
        }
    }"""

    # Tentar ler do grid via JS
    for fr in page.frames:
        try:
            result = fr.evaluate(JS_LER_GRID)
            if result and not result.get('erro') and result.get('rows'):
                _log(f"Grid '{result['gridName']}': {result['recordCount']} registros, {len(result['headers'])} colunas")
                cols = result['headers'] or list(result['rows'][0].keys())
                return pd.DataFrame(result['rows'], columns=cols)
        except Exception:
            continue

    # Ler tabela HTML do relatório (renderizado como HTML real, não wf-grid virtual)
    for fr in page.frames:
        try:
            tabelas = fr.evaluate("""() => {
                // Encontrar a tabela de dados (maior número de colunas em suas linhas)
                const resultado = [];
                for (const tbl of document.querySelectorAll('table')) {
                    const allRows = [...tbl.querySelectorAll('tr')];
                    if (!allRows.length) continue;

                    // Descobrir máximo de células por linha para detectar linha de dados
                    const maxCols = Math.max(...allRows.map(tr => tr.querySelectorAll('td,th').length));
                    if (maxCols < 3) continue;

                    // Encontrar primeira linha que tenha maxCols células (cabeçalho)
                    let headerIdx = -1;
                    let headers = [];
                    for (let i = 0; i < allRows.length; i++) {
                        const cells = allRows[i].querySelectorAll('td,th');
                        if (cells.length === maxCols) {
                            headers = [...cells].map(c => c.textContent.trim());
                            headerIdx = i;
                            break;
                        }
                    }
                    if (headerIdx < 0) continue;

                    // Linhas de dados: após o cabeçalho, com maxCols células
                    const rows = [];
                    for (let i = headerIdx + 1; i < allRows.length; i++) {
                        const cells = [...allRows[i].querySelectorAll('td')];
                        if (cells.length >= maxCols) {
                            rows.push(cells.slice(0, maxCols).map(c => c.textContent.trim()));
                        }
                    }

                    resultado.push({ headers, rows, maxCols, rowCount: rows.length });
                }
                // Ordenar por mais colunas, depois mais linhas
                resultado.sort((a, b) => b.maxCols - a.maxCols || b.rowCount - a.rowCount);
                return resultado;
            }""")

            if tabelas:
                melhor = tabelas[0]
                _log(f"Tabela HTML: {melhor['rowCount']} linha(s), {melhor['maxCols']} coluna(s)")
                hdrs = melhor['headers']
                rows = melhor['rows']
                if rows:
                    # Remover duplicatas no cabeçalho
                    seen = {}
                    hdrs_uniq = []
                    for h in hdrs:
                        h2 = h if h not in seen else f"{h}_{seen[h]}"
                        seen[h] = seen.get(h, 0) + 1
                        hdrs_uniq.append(h2)
                    return pd.DataFrame(rows, columns=hdrs_uniq)
        except Exception:
            continue

    raise RuntimeError("Não foi possível ler dados do relatório em nenhum frame.")


def imprimir_relatorio(df: pd.DataFrame):
    """Imprime o relatório no console de forma legível."""
    total = len(df)
    _log(f"Relatório: {total} linha(s) | {len(df.columns)} coluna(s)")
    print()
    pd.set_option("display.max_rows", None)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 0)
    pd.set_option("display.max_colwidth", 60)
    print(df.to_string(index=False))
    print()
    _log(f"Total de registros: {total}")


# ---------------------------------------------------------------------------
# Upsert no banco de dados
# ---------------------------------------------------------------------------

# Colunas da tabela (sem 'id')
_DB_COLS = [
    "PED_UFPESSOAOP.REGIAO.NOME",
    "PED_PESSOA.UF.CODIGO",
    "PED_OBSERVACAO",
    "PED_PESSOA.LOCALIDADE.CODIGO",
    "PED_CHCRIACAO",
    "PED_EMISSAO",
    "PED_PREVISAOEMISSAODOC",
    "PED_PROGRAMACA",
    "PED_CLASSE.NOME",
    "PED_PESSOA.CODIGO",
    "PED_RECURSO.CODIGO",
    "PED_RECURSO.NOME",
    "PED_RECURSO.CLASSE.NOME",
    "PED_NUMEROSERIE",
    "PED_NUCLEO.CODIGO",
    "PED_QUANTIDADE",
    "PED_UNITARIO",
    "PED_TOTAL",
    "PED_RECURSO.DESCRICAOGENERICA",
    "PED_REPRESENTA.CODIGO",
    "PED_IDNEGOCIACAO",
]

_DATE_COLS    = {"PED_EMISSAO", "PED_PREVISAOEMISSAODOC", "PED_PROGRAMACA"}
_NUMERIC_COLS = {"PED_QUANTIDADE", "PED_UNITARIO", "PED_TOTAL"}
_CONFLICT_KEY = ("PED_CHCRIACAO", "PED_NUMEROSERIE")

_TABELA_PENDENCIA  = "apontamento_v2.comercial_pendencia_importacao_planilha"
# Pedido conferido = existe registro aqui com chave_pedido = PED_CHCRIACAO.
# Pedidos conferidos não podem mais ser alterados pela automação.
_TABELA_CONFERENCIA = "apontamento_v2.comercial_conferenciapedido"

# Mapeamento: label do relatório → nome interno da coluna no banco.
# Os cabeçalhos do relatório são rótulos de exibição, não os nomes internos.
_LABEL_TO_DB: dict[str, str] = {
    "Região":                       "PED_UFPESSOAOP.REGIAO.NOME",
    "Pessoa.UF.Código":             "PED_PESSOA.UF.CODIGO",
    "Observação":                   "PED_OBSERVACAO",
    "Pessoa.Cidade.Código":         "PED_PESSOA.LOCALIDADE.CODIGO",
    "Ch Criação":                   "PED_CHCRIACAO",
    "Emissão":                      "PED_EMISSAO",
    "Previsão Emissão Documento":   "PED_PREVISAOEMISSAODOC",
    "Prog Entrega":                 "PED_PROGRAMACA",
    "Classificação.Nome":           "PED_CLASSE.NOME",
    "Pessoa.Código":                "PED_PESSOA.CODIGO",
    "Recurso.Código":               "PED_RECURSO.CODIGO",
    "Recurso.Nome":                 "PED_RECURSO.NOME",
    "Recurso.Classe.Nome":          "PED_RECURSO.CLASSE.NOME",
    "Número de série":              "PED_NUMEROSERIE",
    "Núcleo.Código":                "PED_NUCLEO.CODIGO",
    "Quantidade":                   "PED_QUANTIDADE",
    "Unitário":                     "PED_UNITARIO",
    "Total":                        "PED_TOTAL",
    "Recurso.Descrição Genérica":   "PED_RECURSO.DESCRICAOGENERICA",
    "Representante.Código":         "PED_REPRESENTA.CODIGO",
    "ID Negociação":                "PED_IDNEGOCIACAO",
}


def _parse_valor(val, db_col: str):
    """Converte um valor do DataFrame para o tipo esperado pelo banco."""
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    s = str(val).strip()
    if s == "":
        return None

    if db_col in _DATE_COLS:
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                pass
        _log(f"AVISO: não foi possível converter '{s}' como data para '{db_col}'")
        return None

    if db_col in _NUMERIC_COLS:
        try:
            # "18.000,0000" → "18000.0000"
            return float(s.replace(".", "").replace(",", "."))
        except ValueError:
            return None

    return s


def _mapear_colunas(df_cols: list) -> dict:
    """
    Retorna {db_col: df_col}.
    Prioridade: 1) match exato  2) label do relatório via _LABEL_TO_DB  3) case-insensitive.
    """
    # Inverso: db_col → label do relatório (para lookup rápido)
    _db_to_label = {v: k for k, v in _LABEL_TO_DB.items()}

    df_set   = set(df_cols)
    df_upper = {c.upper(): c for c in df_cols}
    mapping: dict[str, str] = {}

    for db_col in _DB_COLS:
        # 1. Nome interno igual ao cabeçalho (ex.: relatório exportado via API)
        if db_col in df_set:
            mapping[db_col] = db_col
            continue
        # 2. Rótulo de exibição mapeado
        label = _db_to_label.get(db_col)
        if label and label in df_set:
            mapping[db_col] = label
            continue
        # 3. Case-insensitive (fallback)
        if db_col.upper() in df_upper:
            mapping[db_col] = df_upper[db_col.upper()]

    return mapping


def _garantir_constraint(cur):
    """Garante unique index funcional (COALESCE) para upsert correto com NULLs."""
    cur.execute("""
        DO $$
        BEGIN
            -- Remover constraint simples antiga se ainda existir (criada por versão anterior)
            ALTER TABLE apontamento_v2.comercial_pendencia_importacao_planilha
                DROP CONSTRAINT IF EXISTS uq_ped_chcriacao_numeroserie;

            -- Criar índice funcional apenas se ainda não existir
            IF NOT EXISTS (
                SELECT 1
                FROM   pg_indexes
                WHERE  schemaname = 'apontamento_v2'
                  AND  tablename  = 'comercial_pendencia_importacao_planilha'
                  AND  indexname  = 'uq_ped_chcriacao_numeroserie'
            ) THEN
                -- Eliminar duplicatas usando a mesma lógica COALESCE do índice
                DELETE FROM apontamento_v2.comercial_pendencia_importacao_planilha a
                USING  apontamento_v2.comercial_pendencia_importacao_planilha b
                WHERE  a.id > b.id
                  AND  COALESCE(a."PED_CHCRIACAO",   '') = COALESCE(b."PED_CHCRIACAO",   '')
                  AND  COALESCE(a."PED_NUMEROSERIE", '') = COALESCE(b."PED_NUMEROSERIE", '');

                CREATE UNIQUE INDEX uq_ped_chcriacao_numeroserie
                ON apontamento_v2.comercial_pendencia_importacao_planilha
                (COALESCE("PED_CHCRIACAO", ''), COALESCE("PED_NUMEROSERIE", ''));
            END IF;
        END$$;
    """)

    # Colunas opcionais que podem chegar vazias do relatório — remover NOT NULL
    cur.execute("""
        ALTER TABLE apontamento_v2.comercial_pendencia_importacao_planilha
            ALTER COLUMN "PED_PREVISAOEMISSAODOC"        DROP NOT NULL,
            ALTER COLUMN "PED_PROGRAMACA"                DROP NOT NULL,
            ALTER COLUMN "PED_NUMEROSERIE"               DROP NOT NULL,
            ALTER COLUMN "PED_RECURSO.CODIGO"            DROP NOT NULL,
            ALTER COLUMN "PED_RECURSO.NOME"              DROP NOT NULL,
            ALTER COLUMN "PED_RECURSO.CLASSE.NOME"       DROP NOT NULL,
            ALTER COLUMN "PED_NUCLEO.CODIGO"             DROP NOT NULL,
            ALTER COLUMN "PED_RECURSO.DESCRICAOGENERICA" DROP NOT NULL,
            ALTER COLUMN "PED_REPRESENTA.CODIGO"         DROP NOT NULL,
            ALTER COLUMN "PED_IDNEGOCIACAO"              DROP NOT NULL;
    """)


def upsert_pendencias(df: pd.DataFrame, data_inicio: date, data_fim: date):
    """
    Sincroniza o DataFrame com a tabela
    apontamento_v2.comercial_pendencia_importacao_planilha.

    Em uma única transação:
      1. Apaga os pedidos PENDENTES (ainda não conferidos) com emissão entre
         data_inicio e data_fim — o relatório é a fonte da verdade para essa janela.
      2. Insere o que veio do relatório, ignorando pedidos já conferidos.
    Pedidos conferidos nunca são apagados nem alterados.

    Chave de conflito: (PED_CHCRIACAO, PED_NUMEROSERIE).
    Colunas do DF são mapeadas para o banco por nome exato (ou case-insensitive).
    """
    _log("Iniciando upsert no banco de dados...")

    col_map = _mapear_colunas(list(df.columns))

    unmapped = [c for c in _DB_COLS if c not in col_map]
    if unmapped:
        _log(f"AVISO: colunas sem mapeamento (serão ignoradas): {unmapped}")

    _log(f"Colunas do relatório: {list(df.columns)}")

    # Colunas presentes no mapeamento, na ordem de _DB_COLS
    mapped_db = [c for c in _DB_COLS if c in col_map]
    mapped_df = [col_map[c] for c in mapped_db]

    if not mapped_db:
        raise RuntimeError(
            "Nenhuma coluna do relatório foi reconhecida. "
            f"Verifique _LABEL_TO_DB. Colunas recebidas: {list(df.columns)}"
        )

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )
    try:
        cur = conn.cursor()
        _garantir_constraint(cur)
        conn.commit()

        # Índice funcional usa COALESCE para tratar NULL como '' (NULL != NULL no PostgreSQL)
        conflict_sql = 'COALESCE("PED_CHCRIACAO", \'\'), COALESCE("PED_NUMEROSERIE", \'\')'

        quoted_cols = [f'"{c}"' for c in mapped_db]
        update_set_parts = [
            f'"{c}" = EXCLUDED."{c}"'
            for c in mapped_db
            if c not in _CONFLICT_KEY
        ]
        update_set = ", ".join(update_set_parts)

        sql = f"""
            INSERT INTO {_TABELA_PENDENCIA}
              ({", ".join(quoted_cols)})
            VALUES %s
            ON CONFLICT ({conflict_sql}) DO UPDATE SET
              {update_set}
        """

        # Desduplicar pelo par de colunas-chave antes do upsert para evitar
        # "ON CONFLICT DO UPDATE command cannot affect row a second time"
        key_df_cols = [col_map[c] for c in _CONFLICT_KEY if c in col_map]
        if key_df_cols:
            antes = len(df)
            df = df.drop_duplicates(subset=key_df_cols, keep="last")
            removidos = antes - len(df)
            if removidos:
                _log(f"AVISO: {removidos} linha(s) duplicada(s) removidas antes do upsert.")

        rows = [
            tuple(_parse_valor(row[df_col], db_col) for df_col, db_col in zip(mapped_df, mapped_db))
            for _, row in df.iterrows()
        ]

        # Pedidos já conferidos ficam congelados: não entram no insert/update
        cur.execute(f"SELECT DISTINCT chave_pedido FROM {_TABELA_CONFERENCIA}")
        conferidos = {r[0] for r in cur.fetchall()}
        idx_ch = mapped_db.index("PED_CHCRIACAO") if "PED_CHCRIACAO" in mapped_db else None
        if idx_ch is None:
            raise RuntimeError("Coluna PED_CHCRIACAO não encontrada no relatório; sincronização abortada.")
        ignorados = [r for r in rows if r[idx_ch] in conferidos]
        rows = [r for r in rows if r[idx_ch] not in conferidos]
        if ignorados:
            _log(f"{len(ignorados)} linha(s) de pedidos já conferidos ignoradas.")

        # Apagar pendentes da janela para substituí-los pelo conteúdo atual do relatório
        cur.execute(
            f"""
            DELETE FROM {_TABELA_PENDENCIA} p
            WHERE p."PED_EMISSAO" BETWEEN %s AND %s
              AND NOT EXISTS (
                  SELECT 1 FROM {_TABELA_CONFERENCIA} c
                  WHERE c.chave_pedido = p."PED_CHCRIACAO"
              )
            """,
            (data_inicio, data_fim),
        )
        _log(f"{cur.rowcount} registro(s) pendente(s) apagado(s) entre "
             f"{data_inicio:%d/%m/%Y} e {data_fim:%d/%m/%Y}.")

        if rows:
            execute_values(cur, sql, rows)
        conn.commit()
        _log(f"Sincronização concluída: {len(rows)} registro(s) gravado(s).")

    except Exception as exc:
        conn.rollback()
        raise RuntimeError(f"Erro no upsert: {exc}") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------
def _parse_args():
    parser = argparse.ArgumentParser(description="Automação — Conferência de Pedido")
    parser.add_argument(
        "--data_inicio",
        metavar="DD/MM/YYYY",
        help="Data de início do relatório (padrão: hoje - 7 dias)",
    )
    parser.add_argument(
        "--data_fim",
        metavar="DD/MM/YYYY",
        help="Data de fim do relatório (padrão: hoje)",
    )
    args = parser.parse_args()

    for campo, valor in [("--data_inicio", args.data_inicio), ("--data_fim", args.data_fim)]:
        if valor:
            try:
                datetime.strptime(valor, "%d/%m/%Y")
            except ValueError:
                parser.error(f"{campo}: formato inválido '{valor}'. Use DD/MM/YYYY.")

    return args


def main():
    args = _parse_args()

    # Janela de emissão: a mesma usada no filtro do relatório e na limpeza dos pendentes
    data_inicio = args.data_inicio or (datetime.now() - timedelta(days=7)).strftime("%d/%m/%Y")
    dt_ini = datetime.strptime(data_inicio, "%d/%m/%Y").date()
    dt_fim = datetime.strptime(args.data_fim, "%d/%m/%Y").date() if args.data_fim else date.today()

    _log("Iniciando automação — Conferência de Pedido")
    _log(f"ERP: {ERP_URL} | Usuário: {USERNAME} | Headless: {HEADLESS}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=HEADLESS,
            slow_mo=SLOW_MO,
        )
        context = browser.new_context(viewport={"width": 1600, "height": 900})
        page = context.new_page()

        try:
            login(page)
            navegar_relatorio_pendencia(page)
            preencher_variaveis(page, data_inicio=data_inicio, data_fim=args.data_fim)

            _log("Clicando em Visualizar")
            btn_vis = page.locator("button").filter(has_text="Visualizar").first
            btn_vis.click(timeout=10000)
            aguardar_relatorio_carregar(page)

            df = ler_dados_relatorio(page)
            imprimir_relatorio(df)
            upsert_pendencias(df, dt_ini, dt_fim)

            _log("Concluído.")

        except Exception as e:
            _log(f"ERRO: {e}")
            page.screenshot(path=str(OUTPUT_DIR / "erro.png"), full_page=True)
            raise
        finally:
            browser.close()
            _log("Navegador encerrado.")


if __name__ == "__main__":
    main()
