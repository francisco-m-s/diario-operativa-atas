"""'vol 0' transitorio en un giro (28/09/2026): ATAS escribe 'Position changed … vol 0' y, en el mismo instante,
el volumen del sentido contrario. Ese cero no deja la cuenta plana; contarlo creaba una operación fantasma."""
from atasv4 import almacen, motor

INC = {'espera_min': 5, 'tolerancia_s': 5, 'giro_transitorio_s': 1}


def _con(filas):
    con = almacen.conectar(':memory:')
    for t, v in filas:
        con.execute('INSERT INTO posiciones_hist (cuenta, simbolo, t_log, volumen) VALUES (?,?,?,?)',
                    ('C', 'NQZ6@CME', t, v))
    return con


def test_giro_en_el_mismo_instante_no_es_plano():
    con = _con([('2026-09-23 18:47:28.297', 1), ('2026-09-23 18:48:17.614', 0),
                ('2026-09-23 18:48:17.614', -1), ('2026-09-23 18:49:12.401', 0)])
    assert motor._planos(con, 'C', 'NQZ6@CME', INC) == ['2026-09-23 18:49:12.401']


def test_cierre_parcial_en_el_mismo_instante_si_es_plano():
    # 2 -> 1 -> 0 en el mismo milisegundo: el no nulo es del mismo signo que antes, no hay giro
    con = _con([('2026-09-23 10:00:00.000', 2), ('2026-09-23 10:05:00.100', 1), ('2026-09-23 10:05:00.100', 0)])
    assert motor._planos(con, 'C', 'NQZ6@CME', INC) == ['2026-09-23 10:05:00.100']


def test_reentrada_mismo_sentido_tras_cerrar_si_es_plano():
    con = _con([('2026-09-23 10:00:00.000', 1), ('2026-09-23 10:01:00.000', 0), ('2026-09-23 10:01:00.500', 1)])
    assert motor._planos(con, 'C', 'NQZ6@CME', INC) == ['2026-09-23 10:01:00.000']


def test_giro_fuera_de_la_ventana_si_es_plano():
    con = _con([('2026-09-23 10:00:00.000', 1), ('2026-09-23 10:01:00.000', 0), ('2026-09-23 10:01:03.000', -1)])
    assert motor._planos(con, 'C', 'NQZ6@CME', INC) == ['2026-09-23 10:01:00.000']


def test_sin_regla_se_comporta_como_antes():
    con = _con([('2026-09-23 18:47:28.297', 1), ('2026-09-23 18:48:17.614', 0), ('2026-09-23 18:48:17.614', -1)])
    assert motor._planos(con, 'C', 'NQZ6@CME', None) == ['2026-09-23 18:48:17.614']


def test_giro_no_crea_operacion_fantasma():
    """Caso real APEX-91120-75: largo, giro a corto con una orden de 2, cierre del corto y nuevo largo."""
    con = _con([('2026-09-23 18:47:28.297', 1), ('2026-09-23 18:48:17.614', 0),
                ('2026-09-23 18:48:17.614', -1), ('2026-09-23 18:49:12.401', 0)])

    def fill(fid, oid, lado, px, t_utc, t_log):
        return {'cuenta': 'C', 'simbolo': 'NQZ6@CME', 'fill_id': fid, 'order_id': oid, 'lado': lado, 'precio': px,
                'volumen': 1.0, 't_utc': t_utc, 't_exch': t_utc, 't_log': t_log}
    fills = [fill('1538527', 'A', 'Buy', 30717.0, '2026-09-23 16:47:27.007', '2026-09-23 18:47:28.296'),
             fill('1539522', 'B', 'Sell', 30702.75, '2026-09-23 16:48:16.326', '2026-09-23 18:48:17.612'),
             fill('1539523', 'B', 'Sell', 30702.5, '2026-09-23 16:48:16.326', '2026-09-23 18:48:17.612'),
             fill('1537405', 'C', 'Buy', 30705.5, '2026-09-23 16:49:11.115', '2026-09-23 18:49:12.401')]
    filas, pos = motor.construir_filas(fills, motor._planos(con, 'C', 'NQZ6@CME', INC), '2026-09-23 19:00:00.000',
                                       INC, rescate=lambda *a: [])
    assert pos == 0
    assert [(f.direccion, f.contratos, sum(s['qty'] for s in f.salidas)) for f in filas] == [(1, 1, 1), (-1, 1, 1)]
    assert not any(getattr(f, 'incompleta', False) or getattr(f, 'rescatada', False) for f in filas)
