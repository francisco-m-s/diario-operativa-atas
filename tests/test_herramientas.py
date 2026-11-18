"""Escáner de secretos para publicar sin exponer claves (V5.1). Las pruebas del exportador público están aparte
(test_exportar_publico.py), porque el exportador no viaja a la versión pública.
Las cadenas «sospechosas» se construyen por concatenación para que este mismo fichero no dispare el escáner."""

import os
import re
import sys

from conftest import RAIZ

sys.path.insert(0, os.path.join(RAIZ, 'herramientas'))
import comprobar_secretos as cs  # noqa: E402

BS = chr(92)
CLAVE = '-----BEGIN ' + 'PRIVATE KEY-----'
TOKEN_GITHUB = 'gh' + 'p_' + 'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8'
RUTA = 'C:' + BS + 'Users' + BS + 'Pepe' + BS + 'ATAS'


def tipos(rel, texto, ids=None):
    return [(nivel, linea, tipo) for nivel, linea, tipo in cs.escanear(rel, texto, ids)]


def test_detecta_una_clave_privada_y_un_token_sin_devolver_nunca_el_valor():
    hallazgos = tipos('x.txt', 'hola\n' + CLAVE + '\nabc\n' + TOKEN_GITHUB + '\n')
    assert hallazgos == [('ERROR', 2, 'BLOQUE_CLAVE_PRIVADA'), ('ERROR', 4, 'TOKEN_GITHUB')]
    assert all(len(h) == 3 and not any(CLAVE in str(c) or TOKEN_GITHUB in str(c) for c in h) for h in hallazgos)


def test_detecta_una_cuenta_de_servicio_de_google_y_los_ficheros_de_credenciales():
    assert ('ERROR', 1, 'CUENTA_DE_SERVICIO_GOOGLE') in tipos('x.txt', '{"type": "service' + '_account"}')
    for nombre in ('mi-credencial.txt', 'clave.pem', 'proyecto-1234.json', '.env', 'sub/.env.local'):
        assert ('ERROR', 0, 'NOMBRE_DE_FICHERO_DE_SECRETO') in tipos(nombre, '')


def test_la_asignacion_de_un_secreto_solo_cuenta_entre_comillas_en_el_codigo():
    valor = 'abcdefghijklmnop12345'
    assert tipos('c.yaml', 'api_key: ' + valor) == [('ERROR', 1, 'ASIGNACION_DE_SECRETO')]
    assert tipos('c.py', 'api_key = ' + "'" + valor + "'") == [('ERROR', 1, 'ASIGNACION_DE_SECRETO')]
    assert tipos('c.py', 'api_key = fuente.leer_la_clave_del_entorno()') == []


def test_una_ruta_con_nombre_de_usuario_es_aviso_en_privado_y_error_en_publico():
    assert tipos('a.md', RUTA) == [('AVISO', 1, 'RUTA_CON_NOMBRE_DE_USUARIO')]
    assert tipos('a.md', RUTA, ids=[]) == [('ERROR', 1, 'RUTA_CON_NOMBRE_DE_USUARIO')]
    assert tipos('a.md', 'C:' + BS + 'Users' + BS + 'USUARIO' + BS + 'ATAS') == []


def test_en_modo_publico_un_id_real_que_queda_es_error_pero_no_una_subcadena_de_otra_cosa():
    ids = [re.compile(r'(?<![0-9A-Za-z])' + re.escape('APEX-97321') + r'(?!\d)', re.IGNORECASE)]
    assert tipos('a.md', 'cuenta PA-APEX-97321-16', ids) == [('ERROR', 1, 'ID_REAL_SIN_ANONIMIZAR')]
    assert tipos('a.md', 'cuenta APEX-904726-16', ids) == []
