"""Aviso DESCUADRE del BAL (28/09/2026): la tolerancia entre la hora del log y la del exchange no debe tapar
un descuadre real. El caso del falso positivo (fsern…250, 24/09 18:00:32) se comprobó con la base real."""
from datetime import timedelta

from atasv4 import almacen, fuentes
from test_regresion import AHORA, PA15, _bal


def test_descuadre_real_se_sigue_detectando(entorno):
    ent, con, s = entorno
    assert 'DESCUADRE' not in _bal(s, PA15)['obs']
    t, bal = almacen.ultimo_bal_plano(con, PA15)
    t2 = fuentes.fmt_ts(fuentes.parse_ts(t) + timedelta(seconds=30))
    assert t2 < AHORA
    con.execute('INSERT INTO equity (cuenta, t_log, bal, pnl_abierto, pnl_cerrado) VALUES (?,?,?,?,?)',
                (PA15, t2, round(bal - 100, 2), 0.0, 0.0))
    con.commit()
    assert almacen.ultimo_bal_plano(con, PA15) == (t2, round(bal - 100, 2))
    obs = _bal(s, PA15)['obs']
    assert 'DESCUADRE' in obs and '-100.00' in obs
