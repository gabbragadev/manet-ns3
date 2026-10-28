#!/usr/bin/env python3
"""
Processa os CSVs brutos das simulacoes, valida os dados, calcula as estatisticas
e gera os graficos do artigo.

Uso (a partir da raiz do repositorio):
    python3 scripts/analyze.py
    python3 scripts/analyze.py --compare-speed 20 --ext-speed 10

Entradas:
    data/raw/base/*.csv               etapa 1 (experimento-base: 20 nos, pausa 0 s)
    data/raw/extensao_nos/*.csv       etapa 2 (variavel adicional 1: quantidade de nos)
    data/raw/extensao_pausa/*.csv     etapa 3 (variavel adicional 2: tempo de pausa)
    data/raw/apoio_arp_padrao/*.csv   opcional: etapa 1 com o ARP padrao do ns-3

Saidas:
    data/processed/por_fluxo.csv        todas as linhas brutas reunidas
    data/processed/por_execucao.csv     metricas da rede em cada execucao
    data/processed/estatisticas.csv     media, mediana, minimo, maximo, desvio-padrao e IC 95%
    data/processed/diferencas_pareadas.csv   diferencas entre protocolos no mesmo run
    data/processed/resumo.md            tabelas prontas para leitura
    data/processed/validacao.txt        relatorio de validacao dos dados
    figures/*.png e figures/*.pdf       graficos
    article/figuras/*.pdf               graficos compactos, no tamanho usado no artigo
    article/tabela_base.tex             linhas da tabela de resultados do artigo
"""

import argparse
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, NullFormatter
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
RAW = REPO / "data" / "raw"
PROCESSED = REPO / "data" / "processed"
FIGURES = REPO / "figures"
ARTICLE = REPO / "article"

PROTOCOLS = ["AODV", "OLSR", "DSDV"]
EXPECTED_TYPEID = {
    "AODV": "ns3::aodv::RoutingProtocol",
    "OLSR": "ns3::olsr::RoutingProtocol",
    "DSDV": "ns3::dsdv::RoutingProtocol",
}

# Identidade visual de cada protocolo: cor + marcador + estilo de linha + hachura,
# para que as series continuem distinguiveis em impressao preto e branco.
STYLE = {
    "AODV": dict(color="#2a78d6", marker="o", linestyle="-", hatch=""),
    "OLSR": dict(color="#eb6834", marker="s", linestyle="--", hatch="//"),
    "DSDV": dict(color="#1baf7a", marker="^", linestyle="-.", hatch=".."),
}
INK = "#0b0b0b"
INK_SOFT = "#52514e"
GRID = "#e4e3df"

# metrica -> (rotulo do eixo, titulo curto)
METRICS = {
    "pdrPct": ("PDR (%)", "Taxa de entrega (PDR)"),
    "throughputMbps": ("Throughput da rede (Mbps)", "Throughput"),
    "delayMs": ("Atraso médio fim a fim (ms)", "Atraso fim a fim"),
    "jitterMs": ("Jitter médio (ms)", "Jitter"),
}
DECIMALS = {"pdrPct": 1, "throughputMbps": 3, "delayMs": 1, "jitterMs": 1}
KEY = ["protocol", "speed", "nodes", "pause"]       # uma configuracao experimental
STAGES = ["base", "extensao_nos", "extensao_pausa"]
EXTRA_METRICS = {
    "delayP50Ms": ("Mediana do atraso (ms)", "Atraso típico (mediana)"),
    "delayP95Ms": ("Percentil 95 do atraso (ms)", "Atraso no pior 5%"),
    "avgHops": ("Saltos por pacote entregue", "Número de saltos"),
    "ctrlKbps": ("Tráfego de controle (kbps)", "Custo de controle do roteamento"),
}

# Valor critico t de Student bicaudal para 95% (indice = graus de liberdade).
T95 = [math.nan, 12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
       2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
       2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]


def t95(df: int) -> float:
    if df < 1:
        return math.nan
    return T95[df] if df < len(T95) else 1.96


# ---------------------------------------------------------------------------
# Leitura e validacao
# ---------------------------------------------------------------------------
def load_stage(stage: str) -> pd.DataFrame:
    files = sorted((RAW / stage).glob("manet_*.csv"))
    if not files:
        return pd.DataFrame()
    df = pd.concat((pd.read_csv(f) for f in files), ignore_index=True)
    df.insert(0, "stage", stage)
    return df


def validate(flows: pd.DataFrame, expected_runs: int) -> list[str]:
    """Confere a consistencia dos dados brutos. Devolve as linhas do relatorio."""
    report: list[str] = []
    problems = 0

    def check(ok: bool, message: str, detail: str = "") -> None:
        nonlocal problems
        report.append(f"[{'OK' if ok else 'PROBLEMA'}] {message}")
        if not ok:
            problems += 1
            if detail:
                report.append(detail)

    key = KEY
    runs = flows.groupby(key)["run"].nunique()
    bad = runs[runs != expected_runs]
    check(bad.empty, f"cada configuração tem {expected_runs} repetições "
                     f"({len(runs)} configurações, {int(runs.sum())} execuções)", bad.to_string())

    rows = flows.groupby(key + ["run"]).agg(n=("flowId", "size"), flows=("flows", "first"))
    bad = rows[rows.n != rows.flows]
    check(bad.empty, "cada execução tem uma linha por fluxo", bad.to_string())

    expected_tx = np.round(flows.packetRate * (flows.appStop - flows.appStart))
    bad = flows[flows.appTxPackets != expected_tx]
    check(bad.empty, "pacotes gerados por fluxo = taxa × duração útil "
                     f"({int(expected_tx.iloc[0])} pacotes)",
          bad[key + ["run", "flowId", "appTxPackets"]].to_string())

    bad = flows[(flows.rxPackets > flows.ipTxPackets) | (flows.ipTxPackets > flows.appTxPackets)]
    check(bad.empty, "recebidos ≤ transmitidos na camada IP ≤ gerados pela aplicação",
          bad[key + ["run", "flowId", "appTxPackets", "ipTxPackets", "rxPackets"]].to_string())

    bad = flows[flows.lostPackets != flows.appTxPackets - flows.rxPackets]
    check(bad.empty, "perdidos = gerados − recebidos")

    bad = flows[(flows.pdrPct < 0) | (flows.pdrPct > 100) | flows.pdrPct.isna()]
    check(bad.empty, "PDR entre 0 e 100% em todos os fluxos")

    offered = flows.appTxPackets * (flows.pktSize + 28) * 8 / (flows.appStop - flows.appStart) / 1e6
    bad = flows[(flows.throughputMbps < 0) | (flows.throughputMbps > offered + 1e-9)]
    check(bad.empty, "throughput entre 0 e a carga oferecida")

    bad = flows[(flows.rxPackets > 0) & (flows.delayMs.isna() | (flows.delayMs <= 0))]
    check(bad.empty, "atraso positivo em todo fluxo com recepção")

    bad = flows[(flows.rxPackets > 1) & (flows.jitterMs.isna() | (flows.jitterMs < 0))]
    check(bad.empty, "jitter não negativo em todo fluxo com mais de um pacote recebido")

    bad = flows[flows.timeLastRxS > flows.simTime]
    check(bad.empty, "nenhuma recepção depois do fim da simulação")

    bad = flows[flows.routingTypeId != flows.protocol.map(EXPECTED_TYPEID)]
    check(bad.empty, "protocolo instalado confere com o protocolo pedido")

    bad = flows[(flows.rxPackets > 0) & ((flows.delayP50Ms > flows.delayP95Ms + 1e-9)
                                         | flows.delayP50Ms.isna())]
    check(bad.empty, "mediana do atraso ≤ percentil 95 em todo fluxo com recepção")

    for col in ["areaSide", "pktSize", "packetRate", "txPowerDbm", "arpDeadTimeout", "simTime",
                "appStart", "appStop", "seed", "flows"]:
        values = sorted(flows[col].unique())
        check(len(values) == 1, f"parâmetro constante {col} = {values[0] if len(values) == 1 else values}")

    dead = flows[flows.rxPackets == 0]
    report.append(f"[INFO] fluxos sem nenhuma recepção: {len(dead)} de {len(flows)} "
                  "(entram no PDR e no throughput como zero; não entram no atraso nem no jitter)")
    if not dead.empty:
        report.append(dead.groupby(key).size().rename("fluxos").to_string())

    report.append(f"RESULTADO: {problems} problema(s) encontrado(s)")
    return report


# ---------------------------------------------------------------------------
# Agregacao
# ---------------------------------------------------------------------------
def per_run(flows: pd.DataFrame) -> pd.DataFrame:
    """Metricas da rede em cada execucao (os fluxos de uma execucao sao agregados)."""
    out = []
    cols = ["stage", "protocol", "speed", "nodes", "pause", "seed", "run"]
    for keys, g in flows.groupby(cols):
        rx = g.rxPackets.sum()
        app_tx = g.appTxPackets.sum()
        duration = g.appStop.iloc[0] - g.appStart.iloc[0]
        jitter_samples = (g.rxPackets - 1).clip(lower=0)
        row = dict(zip(cols, keys))
        row.update(
            appTxPackets=app_tx,
            rxPackets=rx,
            lostPackets=app_tx - rx,
            rxBytes=g.rxBytes.sum(),
            flowsWithoutRx=int((g.rxPackets == 0).sum()),
            # throughput da rede: soma dos fluxos sobre a mesma duracao util
            throughputMbps=g.rxBytes.sum() * 8 / duration / 1e6,
            pdrPct=100 * rx / app_tx,
            # medias ponderadas pelo numero de pacotes: equivalem a
            # soma(delaySum) / soma(rxPackets) e soma(jitterSum) / soma(rxPackets - 1)
            delayMs=(g.delayMs * g.rxPackets).sum() / rx if rx > 0 else np.nan,
            jitterMs=((g.jitterMs * jitter_samples).sum() / jitter_samples.sum()
                      if jitter_samples.sum() > 0 else np.nan),
            # mediana e percentil 95 do atraso de todos os pacotes da execucao
            delayP50Ms=g.runDelayP50Ms.iloc[0],
            delayP95Ms=g.runDelayP95Ms.iloc[0],
            avgHops=1 + g.timesForwarded.sum() / rx if rx > 0 else np.nan,
            ctrlTxPackets=g.runCtrlTxPackets.iloc[0],
            ctrlKbps=g.runCtrlTxBytes.iloc[0] * 8 / g.simTime.iloc[0] / 1e3,
        )
        out.append(row)
    return pd.DataFrame(out)


def statistics(runs: pd.DataFrame) -> pd.DataFrame:
    """Media, minimo, maximo, desvio-padrao amostral e IC 95% por configuracao."""
    out = []
    for keys, g in runs.groupby(KEY):
        for metric in list(METRICS) + list(EXTRA_METRICS):
            v = g[metric].dropna()
            n = len(v)
            std = v.std(ddof=1) if n > 1 else np.nan
            out.append(dict(
                protocol=keys[0], speed=keys[1], nodes=keys[2], pause=keys[3], metric=metric, n=n,
                mean=v.mean() if n else np.nan,
                median=v.median() if n else np.nan,
                min=v.min() if n else np.nan,
                max=v.max() if n else np.nan,
                std=std,
                ci95=t95(n - 1) * std / math.sqrt(n) if n > 1 else np.nan,
            ))
    stats = pd.DataFrame(out)
    stats["protocol"] = pd.Categorical(stats.protocol, PROTOCOLS, ordered=True)
    return stats.sort_values(["nodes", "pause", "speed", "protocol", "metric"]).reset_index(drop=True)


PAIRS = [("AODV", "OLSR"), ("AODV", "DSDV"), ("OLSR", "DSDV")]


def paired_differences(runs: pd.DataFrame) -> pd.DataFrame:
    """
    Diferenca entre dois protocolos no MESMO run (mesmas trajetorias e mesmos instantes de
    inicio dos fluxos). A variacao causada pela topologia sorteada se cancela, e o intervalo
    de confianca da diferenca fica bem mais estreito que o das medias separadas.
    """
    out = []
    wide_cols = ["speed", "nodes", "pause", "run"]
    for metric in METRICS:
        wide = runs.pivot_table(index=wide_cols, columns="protocol", values=metric)
        for a, b in PAIRS:
            d = (wide[a] - wide[b]).dropna().rename("diff").reset_index()
            for keys, g in d.groupby(["speed", "nodes", "pause"]):
                v = g["diff"]
                n = len(v)
                std = v.std(ddof=1) if n > 1 else np.nan
                ci = t95(n - 1) * std / math.sqrt(n) if n > 1 else np.nan
                out.append(dict(metric=metric, pair=f"{a} − {b}", a=a, b=b,
                                speed=keys[0], nodes=keys[1], pause=keys[2], n=n,
                                mean=v.mean(), std=std, ci95=ci,
                                significant=bool(abs(v.mean()) > ci) if n > 1 else False))
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# Graficos
# ---------------------------------------------------------------------------
def setup_style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.titlesize": 11,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "axes.labelcolor": INK_SOFT,
        "axes.edgecolor": INK_SOFT,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "xtick.color": INK_SOFT,
        "ytick.color": INK_SOFT,
        "text.color": INK,
        "legend.frameon": False,
        "figure.facecolor": "white",
        "savefig.facecolor": "white",
        "savefig.dpi": 300,
        "savefig.bbox": "tight",
    })


def save(fig, name: str) -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURES / f"{name}.png")
    fig.savefig(FIGURES / f"{name}.pdf")
    plt.close(fig)
    print(f"  figures/{name}.png")


def draw_lines(ax, stats: pd.DataFrame, metric: str, x: str, x_label: str) -> None:
    """Uma serie por protocolo, com media e barra de erro (IC 95%)."""
    data = stats[stats.metric == metric]
    xs = sorted(data[x].unique())
    # pequeno deslocamento horizontal para as barras de erro nao se sobreporem
    dodge = 0.012 * (max(xs) - min(xs)) if len(xs) > 1 else 0
    for i, protocol in enumerate(PROTOCOLS):
        d = data[data.protocol == protocol].sort_values(x)
        s = STYLE[protocol]
        # barras de erro truncadas nos limites fisicos da metrica (0 e, no PDR, 100%)
        upper_limit = 100.0 if metric == "pdrPct" else np.inf
        yerr = np.vstack([np.minimum(d["ci95"], d["mean"]),
                          np.minimum(d["ci95"], upper_limit - d["mean"])])
        ax.errorbar(d[x] + (i - 1) * dodge, d["mean"], yerr=yerr, label=protocol,
                    color=s["color"], marker=s["marker"], linestyle=s["linestyle"],
                    linewidth=2, markersize=7, markeredgecolor="white", markeredgewidth=1,
                    capsize=3, elinewidth=1.2)
    ax.set_xticks(xs)
    ax.set_xlabel(x_label)
    ax.set_ylabel(METRICS.get(metric, EXTRA_METRICS.get(metric))[0])
    if metric == "pdrPct":
        ax.set_ylim(0, 105)
    else:
        ax.set_ylim(bottom=0)


def base_only(df: pd.DataFrame, args) -> pd.DataFrame:
    """Recorte do experimento-base: 20 nos, pausa 0 s."""
    return df[(df.nodes == args.base_nodes) & (df.pause == 0)]


def plain_log_axis(ax) -> None:
    """Eixo y em escala log com rotulos comuns (1, 2, 5, 10...) em vez de potencias."""
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.yaxis.set_minor_formatter(NullFormatter())


def fig_metric_vs_speed(stats: pd.DataFrame, metric: str, name: str, runs: int) -> None:
    fig, ax = plt.subplots(figsize=(5.2, 3.6))
    draw_lines(ax, stats, metric, "speed", "Velocidade dos nós (m/s)")
    title = METRICS.get(metric, EXTRA_METRICS.get(metric))[1]
    ax.set_title(f"{title} × velocidade")
    ax.legend(title=f"média de {runs} execuções, IC 95%", title_fontsize=8, fontsize=9)
    save(fig, name)


def fig_panel(stats: pd.DataFrame, x: str, x_label: str, name: str, suptitle: str) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(9, 6.4))
    for ax, metric in zip(axes.flat, METRICS):
        draw_lines(ax, stats, metric, x, x_label)
        ax.set_title(METRICS[metric][1])
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.suptitle(suptitle, x=0.01, y=0.995, ha="left", fontsize=12, fontweight="bold")
    fig.legend(handles, labels, loc="upper left", ncol=3, fontsize=9, bbox_to_anchor=(0.0, 0.965))
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    save(fig, name)


def fig_compare(runs: pd.DataFrame, stats: pd.DataFrame, speed: float, name: str, n_runs: int) -> None:
    """Comparativo agregado entre os protocolos em uma condicao de mobilidade."""
    fig, axes = plt.subplots(1, 4, figsize=(11, 3.4))
    sel_stats = stats[stats.speed == speed]
    sel_runs = runs[runs.speed == speed]
    for ax, metric in zip(axes, METRICS):
        d = sel_stats[sel_stats.metric == metric].set_index("protocol")
        highest = 0.0
        for i, protocol in enumerate(PROTOCOLS):
            s = STYLE[protocol]
            mean, ci = d.loc[protocol, "mean"], d.loc[protocol, "ci95"]
            ax.bar(i, mean, width=0.62, color=s["color"], hatch=s["hatch"],
                   edgecolor="white", linewidth=1)
            upper_limit = 100.0 if metric == "pdrPct" else np.inf
            ax.errorbar(i, mean, yerr=[[min(ci, mean)], [min(ci, upper_limit - mean)]],
                        color=INK, capsize=4, elinewidth=1.2, zorder=4)
            # cada ponto e uma execucao: mostra a variabilidade entre repeticoes
            points = sel_runs[sel_runs.protocol == protocol][metric].dropna()
            ax.scatter(np.full(len(points), i) + np.linspace(-0.22, 0.22, len(points)), points,
                       s=10, color="white", edgecolor=INK, linewidth=0.6, zorder=3)
            top = np.nanmax([min(mean + ci, upper_limit), points.max()])
            highest = max(highest, top)
            ax.annotate(f"{mean:.{DECIMALS[metric]}f}", (i, top), xytext=(0, 4),
                        textcoords="offset points", ha="center", fontsize=9, color=INK)
        ax.set_xticks(range(len(PROTOCOLS)), PROTOCOLS)
        ax.set_ylabel(METRICS[metric][0])
        ax.set_title(METRICS[metric][1])
        ax.grid(axis="x", visible=False)
        ax.set_ylim(0, highest * 1.15)  # folga para o rotulo acima da barra
    fig.suptitle(f"Comparativo entre protocolos a {speed:g} m/s "
                 f"(barra: média; haste: IC 95%; pontos: as {n_runs} execuções)",
                 x=0.01, ha="left", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    save(fig, name)


def fig_pdr_by_param(stats: pd.DataFrame, param: str, fmt: str, name: str, title: str) -> None:
    """PDR x velocidade em paineis, um por valor do parametro (mesma escala)."""
    values = sorted(stats[param].unique())
    fig, axes = plt.subplots(1, len(values), figsize=(2.9 * len(values), 3.3), sharey=True)
    for ax, value in zip(np.atleast_1d(axes), values):
        draw_lines(ax, stats[stats[param] == value], "pdrPct", "speed", "Velocidade (m/s)")
        ax.set_title(fmt.format(value))
        if ax is not np.atleast_1d(axes)[0]:
            ax.set_ylabel("")
    handles, labels = np.atleast_1d(axes)[0].get_legend_handles_labels()
    fig.suptitle(title, x=0.01, y=0.995, ha="left", fontsize=12, fontweight="bold")
    fig.legend(handles, labels, loc="upper left", ncol=3, fontsize=9, bbox_to_anchor=(0.0, 0.93))
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    save(fig, name)


def fig_delay_distribution(stats: pd.DataFrame, name: str, n_runs: int) -> None:
    """Media, mediana e percentil 95 do atraso: mostra a cauda longa da distribuicao."""
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.4), sharey=True)
    for ax, protocol in zip(axes, PROTOCOLS):
        st = STYLE[protocol]
        for metric, label, ls, mk in [("delayP95Ms", "percentil 95", ":", "v"),
                                      ("delayMs", "média", "-", "o"),
                                      ("delayP50Ms", "mediana", "--", "s")]:
            d = stats[(stats.protocol == protocol) & (stats.metric == metric)].sort_values("speed")
            ax.plot(d.speed, d["mean"], color=st["color"], linestyle=ls, marker=mk,
                    linewidth=2, markersize=6, markeredgecolor="white", label=label)
        plain_log_axis(ax)
        ax.set_xticks(sorted(stats.speed.unique()))
        ax.set_xlabel("Velocidade dos nós (m/s)")
        ax.set_title(protocol)
    axes[0].set_ylabel("Atraso fim a fim (ms, escala log)")
    axes[0].legend(fontsize=9)
    fig.suptitle(f"Distribuição do atraso: média de {n_runs} execuções de cada estatística",
                 x=0.01, ha="left", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    save(fig, name)


def fig_paired(diffs: pd.DataFrame, name: str, n_runs: int) -> None:
    """Diferencas pareadas de PDR e throughput entre protocolos, por velocidade."""
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    colors = {"AODV − OLSR": STYLE["OLSR"]["color"], "AODV − DSDV": STYLE["DSDV"]["color"],
              "OLSR − DSDV": INK_SOFT}
    markers = {"AODV − OLSR": "s", "AODV − DSDV": "^", "OLSR − DSDV": "D"}
    for ax, metric in zip(axes, ["pdrPct", "throughputMbps"]):
        d = diffs[diffs.metric == metric]
        xs = sorted(d.speed.unique())
        dodge = 0.012 * (max(xs) - min(xs))
        for i, (pair, g) in enumerate(d.groupby("pair", sort=False)):
            g = g.sort_values("speed")
            ax.errorbar(g.speed + (i - 1) * dodge, g["mean"], yerr=g["ci95"], label=pair,
                        color=colors[pair], marker=markers[pair], linewidth=2, markersize=6,
                        markeredgecolor="white", capsize=3)
        ax.axhline(0, color=INK_SOFT, linewidth=1)
        ax.set_xticks(xs)
        ax.set_xlabel("Velocidade dos nós (m/s)")
        unit = "pontos percentuais" if metric == "pdrPct" else "Mbps"
        ax.set_ylabel(f"Diferença no mesmo run ({unit})")
        ax.set_title(METRICS[metric][1])
    axes[0].legend(fontsize=9)
    fig.suptitle(f"Diferenças pareadas entre protocolos ({n_runs} runs, IC 95%; "
                 "acima de zero = o primeiro entrega mais)",
                 x=0.01, ha="left", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    save(fig, name)


# ---------------------------------------------------------------------------
# Artefatos do artigo: mesmas estatisticas, graficos no tamanho final da pagina
# (15 cm de largura util no modelo SBC) e sem titulo, porque a legenda fica no LaTeX.
# ---------------------------------------------------------------------------
ARTICLE_RC = {"font.size": 7.5, "axes.titlesize": 8, "axes.labelsize": 7.5,
              "xtick.labelsize": 7, "ytick.labelsize": 7, "legend.fontsize": 7.5,
              "grid.linewidth": 0.5, "axes.linewidth": 0.6}
SHORT_LABEL = {"pdrPct": "PDR (%)", "throughputMbps": "Throughput (Mbps)",
               "delayMs": "Atraso (ms)", "jitterMs": "Jitter (ms)"}
TEXT_WIDTH_IN = 5.9


def draw_lines_compact(ax, stats: pd.DataFrame, metric: str, x: str) -> None:
    data = stats[stats.metric == metric]
    xs = sorted(data[x].unique())
    dodge = 0.012 * (max(xs) - min(xs))
    upper_limit = 100.0 if metric == "pdrPct" else np.inf
    for i, protocol in enumerate(PROTOCOLS):
        d = data[data.protocol == protocol].sort_values(x)
        st = STYLE[protocol]
        yerr = np.vstack([np.minimum(d["ci95"], d["mean"]),
                          np.minimum(d["ci95"], upper_limit - d["mean"])])
        ax.errorbar(d[x] + (i - 1) * dodge, d["mean"], yerr=yerr, label=protocol,
                    color=st["color"], marker=st["marker"], linestyle=st["linestyle"],
                    linewidth=1.3, markersize=4.5, markeredgecolor="white", markeredgewidth=0.6,
                    capsize=2, elinewidth=0.8, capthick=0.8)
    ax.set_xticks(xs)
    ax.set_ylim(0, 105) if metric == "pdrPct" else ax.set_ylim(bottom=0)


def save_article(fig, name: str) -> None:
    (ARTICLE / "figuras").mkdir(parents=True, exist_ok=True)
    fig.savefig(ARTICLE / "figuras" / f"{name}.pdf")
    plt.close(fig)
    print(f"  article/figuras/{name}.pdf")


def article_outputs(runs: pd.DataFrame, stats: pd.DataFrame, args) -> None:
    base_stats = base_only(stats, args)
    base_runs = base_only(runs, args)
    with plt.rc_context(ARTICLE_RC):
        # Figura: as quatro metricas x velocidade + a mediana do atraso no painel do atraso
        fig, axes = plt.subplots(2, 2, figsize=(TEXT_WIDTH_IN, 2.25))
        for ax, metric in zip(axes.flat, METRICS):
            draw_lines_compact(ax, base_stats, metric, "speed")
            ax.set_ylabel(SHORT_LABEL[metric])
        for ax in axes[1]:
            ax.set_xlabel("Velocidade dos nós (m/s)")
        handles, labels = axes[0, 0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="upper center", ncol=3, bbox_to_anchor=(0.5, 1.0))
        fig.tight_layout(rect=(0, 0, 1, 0.94), h_pad=0.5, w_pad=1.0)
        save_article(fig, "fig_base")

        # Figura: comparativo agregado na velocidade escolhida
        speed = args.compare_speed
        fig, axes = plt.subplots(1, 4, figsize=(TEXT_WIDTH_IN, 1.5))
        sel_stats = base_stats[base_stats.speed == speed]
        sel_runs = base_runs[base_runs.speed == speed]
        for ax, metric in zip(axes, METRICS):
            d = sel_stats[sel_stats.metric == metric].set_index("protocol")
            highest = 0.0
            upper_limit = 100.0 if metric == "pdrPct" else np.inf
            for i, protocol in enumerate(PROTOCOLS):
                st = STYLE[protocol]
                mean, ci = d.loc[protocol, "mean"], d.loc[protocol, "ci95"]
                ax.bar(i, mean, width=0.62, color=st["color"], hatch=st["hatch"],
                       edgecolor="white", linewidth=0.6)
                ax.errorbar(i, mean, yerr=[[min(ci, mean)], [min(ci, upper_limit - mean)]],
                            color=INK, capsize=2.5, elinewidth=0.8, capthick=0.8, zorder=4)
                points = sel_runs[sel_runs.protocol == protocol][metric].dropna()
                ax.scatter(np.full(len(points), i) + np.linspace(-0.22, 0.22, len(points)), points,
                           s=3, color="white", edgecolor=INK, linewidth=0.4, zorder=3)
                top = np.nanmax([min(mean + ci, upper_limit), points.max()])
                highest = max(highest, top)
                ax.annotate(f"{mean:.{DECIMALS[metric]}f}".replace(".", ","), (i, top),
                            xytext=(0, 2), textcoords="offset points", ha="center",
                            fontsize=6.5, color=INK)
            ax.set_xticks(range(len(PROTOCOLS)), PROTOCOLS, fontsize=6)
            ax.set_ylabel(SHORT_LABEL[metric])
            ax.grid(axis="x", visible=False)
            ax.set_ylim(0, highest * 1.17)
        fig.tight_layout(w_pad=0.8)
        save_article(fig, "fig_comparativo")

        # Figura: as duas variaveis adicionais, PDR x velocidade (linha 1: nos; linha 2: pausa)
        nodes_stats = stats[stats.pause == 0]
        pause_stats = stats[stats.nodes == args.base_nodes]
        nodes_list = sorted(nodes_stats.nodes.unique())
        pause_list = sorted(pause_stats.pause.unique())
        if len(nodes_list) > 1 and len(pause_list) > 1:
            fig, axes = plt.subplots(2, 4, figsize=(TEXT_WIDTH_IN, 2.35), sharey=True)
            for ax, nodes in zip(axes[0], nodes_list):
                draw_lines_compact(ax, nodes_stats[nodes_stats.nodes == nodes], "pdrPct", "speed")
                ax.set_title(f"{nodes} nós (pausa 0 s)", fontweight="normal", loc="center")
            for ax, pause in zip(axes[1], pause_list):
                draw_lines_compact(ax, pause_stats[pause_stats.pause == pause], "pdrPct", "speed")
                ax.set_title(f"pausa {pause:g} s (20 nós)", fontweight="normal", loc="center")
                ax.set_xlabel("Velocidade (m/s)")
            for row in axes:
                row[0].set_ylabel("PDR (%)")
            handles, labels = axes[0, 0].get_legend_handles_labels()
            fig.legend(handles, labels, loc="upper center", ncol=3, bbox_to_anchor=(0.5, 1.0))
            fig.tight_layout(rect=(0, 0, 1, 0.94), h_pad=0.6, w_pad=0.5)
            save_article(fig, "fig_extensao")

        # Figura: diferencas pareadas de PDR + distribuicao do atraso (lado a lado)
        diffs = paired_differences(base_runs)
        fig, axes = plt.subplots(1, 2, figsize=(TEXT_WIDTH_IN, 1.6))
        ax = axes[0]
        d = diffs[diffs.metric == "pdrPct"]
        xs = sorted(d.speed.unique())
        dodge = 0.012 * (max(xs) - min(xs))
        colors = {"AODV − OLSR": STYLE["OLSR"]["color"], "AODV − DSDV": STYLE["DSDV"]["color"],
                  "OLSR − DSDV": INK_SOFT}
        markers = {"AODV − OLSR": "s", "AODV − DSDV": "^", "OLSR − DSDV": "D"}
        for i, (pair, g) in enumerate(d.groupby("pair", sort=False)):
            g = g.sort_values("speed")
            ax.errorbar(g.speed + (i - 1) * dodge, g["mean"], yerr=g["ci95"], label=pair,
                        color=colors[pair], marker=markers[pair], linewidth=1.2, markersize=4,
                        markeredgecolor="white", capsize=2, elinewidth=0.8)
        ax.axhline(0, color=INK_SOFT, linewidth=0.8)
        ax.set_xticks(xs)
        ax.set_xlabel("Velocidade dos nós (m/s)")
        ax.set_ylabel("Diferença de PDR (p.p.)")
        ax.legend(fontsize=6.5, loc="upper left")
        ax = axes[1]
        for protocol in PROTOCOLS:
            st = STYLE[protocol]
            for metric, ls in [("delayMs", "-"), ("delayP50Ms", ":")]:
                g = base_stats[(base_stats.protocol == protocol) & (base_stats.metric == metric)]
                g = g.sort_values("speed")
                ax.plot(g.speed, g["mean"], color=st["color"], linestyle=ls, marker=st["marker"],
                        linewidth=1.2, markersize=4, markeredgecolor="white",
                        label=f"{protocol} {'média' if metric == 'delayMs' else 'mediana'}")
        plain_log_axis(ax)
        ax.set_yticks([1, 2, 5, 10, 20, 50])
        ax.set_xticks(xs)
        ax.set_xlabel("Velocidade dos nós (m/s)")
        ax.set_ylabel("Atraso (ms, log)")
        from matplotlib.lines import Line2D
        handles = [Line2D([], [], color=STYLE[p]["color"], marker=STYLE[p]["marker"],
                          linestyle="none", markersize=4, label=p) for p in PROTOCOLS]
        handles += [Line2D([], [], color=INK_SOFT, linestyle="-", linewidth=1.2, label="média"),
                    Line2D([], [], color=INK_SOFT, linestyle=":", linewidth=1.2, label="mediana")]
        ax.legend(handles=handles, fontsize=6, ncol=5, loc="lower center",
                  bbox_to_anchor=(0.5, 1.0), handlelength=1.6, columnspacing=0.8, frameon=False)
        fig.tight_layout(w_pad=1.2)
        save_article(fig, "fig_pareado_atraso")

    # Tabela de resultados: media (desvio-padrao) por velocidade e protocolo,
    # com a mediana do atraso na ultima coluna
    def cell(row, decimals):
        return (f"{row['mean']:.{decimals}f} ({row['std']:.{decimals}f})").replace(".", ",")

    lines = []
    cols = list(METRICS) + ["delayP50Ms"]
    dec = {**DECIMALS, "delayP50Ms": 2}
    for speed in sorted(base_stats.speed.unique()):
        for j, protocol in enumerate(PROTOCOLS):
            cells = []
            for metric in cols:
                r = base_stats[(base_stats.speed == speed) & (base_stats.protocol == protocol)
                               & (base_stats.metric == metric)].iloc[0]
                cells.append(cell(r, dec[metric]))
            first = f"\\multirow{{3}}{{*}}{{{speed:g}}}" if j == 0 else ""
            lines.append(f"{first} & {protocol} & " + " & ".join(cells) + r" \\")
        lines.append(r"\midrule")
    lines[-1] = r"\bottomrule"
    (ARTICLE / "tabela_base.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("  article/tabela_base.tex")


# ---------------------------------------------------------------------------
# Resumo em Markdown
# ---------------------------------------------------------------------------
def markdown_table(stats: pd.DataFrame, row_key: str, row_label: str) -> list[str]:
    lines = []
    for metric, (label, _) in {**METRICS, **EXTRA_METRICS}.items():
        lines += [f"**{label}**", "", f"| {row_label} | " + " | ".join(PROTOCOLS) + " |",
                  "|---|" + "---|" * len(PROTOCOLS)]
        d = stats[stats.metric == metric]
        for value in sorted(d[row_key].unique()):
            cells = []
            for protocol in PROTOCOLS:
                r = d[(d[row_key] == value) & (d.protocol == protocol)].iloc[0]
                cells.append(f"{r['mean']:.3f} ± {r['ci95']:.3f} (mediana {r['median']:.3f}; "
                             f"dp {r['std']:.3f}; {r['min']:.3f} a {r['max']:.3f})")
            lines.append(f"| {value:g} | " + " | ".join(cells) + " |")
        lines.append("")
    return lines


def markdown_paired(diffs: pd.DataFrame, title: str) -> list[str]:
    lines = [title, "",
             "Diferença média entre os protocolos no mesmo run ± IC 95%. "
             "Marcado com * quando o intervalo não inclui zero.", "",
             "| Métrica | Velocidade (m/s) | " + " | ".join(f"{a} − {b}" for a, b in PAIRS) + " |",
             "|---|---|" + "---|" * len(PAIRS)]
    for metric in METRICS:
        d = diffs[diffs.metric == metric]
        for speed in sorted(d.speed.unique()):
            cells = []
            for a, b in PAIRS:
                r = d[(d.speed == speed) & (d.pair == f"{a} − {b}")].iloc[0]
                star = " *" if r["significant"] else ""
                cells.append(f"{r['mean']:+.3f} ± {r['ci95']:.3f}{star}")
            lines.append(f"| {METRICS[metric][0]} | {speed:g} | " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--runs", type=int, default=10, help="repetições esperadas por configuração")
    parser.add_argument("--base-nodes", type=int, default=20, help="quantidade de nós do experimento-base")
    parser.add_argument("--compare-speed", type=float, default=20,
                        help="velocidade (m/s) do gráfico comparativo agregado")
    parser.add_argument("--ext-speed", type=float, default=10,
                        help="velocidade (m/s) dos painéis das variáveis adicionais")
    args = parser.parse_args()

    loaded = {stage: load_stage(stage) for stage in STAGES}
    if loaded["base"].empty:
        print("ERRO: nenhum CSV em data/raw/base. Rode scripts/run_batch.sh base primeiro.", file=sys.stderr)
        return 1
    flows = pd.concat([df for df in loaded.values() if not df.empty], ignore_index=True)

    PROCESSED.mkdir(parents=True, exist_ok=True)
    setup_style()

    print("Validação dos dados brutos")
    report = validate(flows, args.runs)
    (PROCESSED / "validacao.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join("  " + line for line in report))

    runs = per_run(flows)
    stats = statistics(runs)
    base_runs = base_only(runs, args)
    diffs = paired_differences(runs)
    flows.to_csv(PROCESSED / "por_fluxo.csv", index=False)
    runs.to_csv(PROCESSED / "por_execucao.csv", index=False, float_format="%.6f")
    stats.to_csv(PROCESSED / "estatisticas.csv", index=False, float_format="%.6f")
    diffs.to_csv(PROCESSED / "diferencas_pareadas.csv", index=False, float_format="%.6f")

    base_stats = base_only(stats, args)
    r = args.runs

    print("Gráficos")
    fig_metric_vs_speed(base_stats, "pdrPct", "fig1_pdr_x_velocidade", r)
    fig_metric_vs_speed(base_stats, "throughputMbps", "fig2_throughput_x_velocidade", r)
    fig_metric_vs_speed(base_stats, "delayMs", "fig3_delay_x_velocidade", r)
    fig_metric_vs_speed(base_stats, "jitterMs", "fig4_jitter_x_velocidade", r)
    fig_compare(base_runs, base_stats, args.compare_speed,
                f"fig5_comparativo_v{args.compare_speed:g}", r)
    fig_panel(base_stats, "speed", "Velocidade dos nós (m/s)", "painel_base",
              f"Experimento-base: {args.base_nodes} nós, pausa 0 s, média de {r} execuções, IC 95%")
    fig_delay_distribution(base_stats, "fig10_atraso_media_mediana_p95", r)
    fig_paired(diffs[(diffs.nodes == args.base_nodes) & (diffs.pause == 0)],
               "fig11_diferencas_pareadas", r)
    fig_metric_vs_speed(base_stats, "ctrlKbps", "apoio_controle_x_velocidade", r)
    fig_metric_vs_speed(base_stats, "avgHops", "apoio_saltos_x_velocidade", r)

    summary = ["# Resumo estatístico", "",
               "Cada célula: média ± IC 95% (mediana; dp = desvio-padrão amostral; mínimo a máximo) "
               f"sobre {r} execuções.", "",
               f"## Etapa 1: experimento-base ({args.base_nodes} nós, pausa 0 s), por velocidade (m/s)", ""]
    summary += markdown_table(base_stats, "speed", "Velocidade (m/s)")
    summary += markdown_paired(diffs[(diffs.nodes == args.base_nodes) & (diffs.pause == 0)],
                               "## Etapa 1: diferenças pareadas entre protocolos")

    nodes_stats = stats[stats.pause == 0]
    if nodes_stats.nodes.nunique() > 1:
        fig_panel(nodes_stats[nodes_stats.speed == args.ext_speed], "nodes", "Quantidade de nós",
                  f"fig6_extensao_nos_v{args.ext_speed:g}",
                  f"Variável adicional 1: quantidade de nós a {args.ext_speed:g} m/s, "
                  f"média de {r} execuções, IC 95%")
        fig_pdr_by_param(nodes_stats, "nodes", "{:g} nós", "fig7_extensao_nos_pdr_x_velocidade",
                         f"PDR × velocidade para cada quantidade de nós (média de {r} execuções, IC 95%)")
        for speed in sorted(nodes_stats.speed.unique()):
            summary += [f"## Etapa 2: quantidade de nós, a {speed:g} m/s", ""]
            summary += markdown_table(nodes_stats[nodes_stats.speed == speed], "nodes", "Nós")

    pause_stats = stats[stats.nodes == args.base_nodes]
    if pause_stats.pause.nunique() > 1:
        fig_panel(pause_stats[pause_stats.speed == args.ext_speed], "pause", "Tempo de pausa (s)",
                  f"fig8_extensao_pausa_v{args.ext_speed:g}",
                  f"Variável adicional 2: tempo de pausa a {args.ext_speed:g} m/s, "
                  f"média de {r} execuções, IC 95%")
        fig_pdr_by_param(pause_stats, "pause", "pausa {:g} s", "fig9_extensao_pausa_pdr_x_velocidade",
                         f"PDR × velocidade para cada tempo de pausa (média de {r} execuções, IC 95%)")
        for speed in sorted(pause_stats.speed.unique()):
            summary += [f"## Etapa 3: tempo de pausa, a {speed:g} m/s", ""]
            summary += markdown_table(pause_stats[pause_stats.speed == speed], "pause", "Pausa (s)")

    arp = load_stage("apoio_arp_padrao")
    if not arp.empty:
        arp_stats = statistics(per_run(arp))
        summary += ["## Apoio: efeito do ajuste do ARP no PDR (%)", "",
                    "Etapa 1 repetida com o tempo de bloqueio padrão do ARP (100 s). "
                    "Cada célula: média ± IC 95%.", "",
                    "| Velocidade (m/s) | Protocolo | ARP 1 s (adotado) | ARP 100 s (padrão) | Diferença |",
                    "|---|---|---|---|---|"]
        a = base_stats[base_stats.metric == "pdrPct"]
        b = arp_stats[arp_stats.metric == "pdrPct"]
        for speed in sorted(a.speed.unique()):
            for protocol in PROTOCOLS:
                ra = a[(a.speed == speed) & (a.protocol == protocol)].iloc[0]
                rb = b[(b.speed == speed) & (b.protocol == protocol)].iloc[0]
                summary.append(f"| {speed:g} | {protocol} | {ra['mean']:.1f} ± {ra['ci95']:.1f} | "
                               f"{rb['mean']:.1f} ± {rb['ci95']:.1f} | {ra['mean'] - rb['mean']:+.1f} |")
        summary.append("")
        arp_stats.to_csv(PROCESSED / "apoio_arp_padrao_estatisticas.csv", index=False, float_format="%.6f")

    if ARTICLE.is_dir():
        print("Artefatos do artigo")
        article_outputs(runs, stats, args)

    (PROCESSED / "resumo.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("Tabelas em data/processed/ (por_fluxo.csv, por_execucao.csv, estatisticas.csv, "
          "diferencas_pareadas.csv, resumo.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
