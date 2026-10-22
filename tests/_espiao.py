"""Espião temporário: imprime o que sobra de registries depois de cada teste."""

def pytest_runtest_teardown(item):
    import app
    vivo = len(app._live_sessions)
    if vivo or app._chat_dsh_livres or app._jobs:
        print(f"\n[espiao] {item.name}: live={vivo} dsh={len(app._chat_dsh_livres)} "
              f"jobs={len(app._jobs)}", flush=True)
