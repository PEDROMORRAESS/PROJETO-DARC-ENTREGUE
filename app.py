import streamlit as st
import ee
import folium
from streamlit_folium import st_folium
import geopandas as gpd
import zipfile
import tempfile
import os
from datetime import date, datetime
import pandas as pd
from io import BytesIO
import io
from fpdf import FPDF
import json
import traceback
import re
import csv
from shapely.ops import unary_union, orient as shapely_orient
from shapely.geometry import Polygon, MultiPolygon
import shapely

APP_VERSION = "v1.0.14"

# Configurar página
st.set_page_config(
    page_title="DARC - IFRO",
    page_icon="🌳",
    layout="wide"
)

# Função para inicializar GEE apenas quando necessário (LAZY LOADING)
def inicializar_gee():
    """Inicializa o Google Earth Engine apenas uma vez"""
    if 'gee_initialized' not in st.session_state:
        with st.spinner("🌍 Conectando ao Google Earth Engine..."):
            try:
                if 'earth_engine' in st.secrets:
                    # Streamlit Cloud: usar Service Account dos secrets
                    # Usa google.oauth2 diretamente para evitar ambiguidade de tipos
                    # do wrapper ee.ServiceAccountCredentials (espera str, não dict)
                    from google.oauth2 import service_account as _google_sa
                    from ee.oauth import SCOPES as _EE_SCOPES
                    service_account_info = json.loads(st.secrets['earth_engine']['service_account'])
                    credentials = _google_sa.Credentials.from_service_account_info(
                        service_account_info, scopes=_EE_SCOPES
                    )
                    ee.Initialize(credentials)
                else:
                    # Desenvolvimento local: autenticação padrão
                    ee.Initialize(project='graceful-fin-479914-k9')

                st.session_state.gee_initialized = True
            except Exception as e:
                st.error(f"❌ Erro ao conectar GEE: {e}")
                st.error("💡 Verifique sua conexão com a internet e tente novamente.")
                st.code(traceback.format_exc())
                st.stop()

def obter_roi():
    """Obtém ou cria o ROI (Region of Interest) do GEE"""
    if 'roi' not in st.session_state or st.session_state.roi is None:
        if st.session_state.gdf is not None:
            inicializar_gee()  # Garante que GEE está inicializado
            geom = st.session_state.gdf.geometry.iloc[0]
            st.session_state.roi = _geom_para_gee(geom)
    return st.session_state.roi


def limpar_gdf_para_folium(gdf):
    """
    Remove colunas problemáticas (Timestamp, datetime) do GeoDataFrame
    para evitar erro 'Timestamp is not JSON serializable' no folium
    """
    if gdf is None:
        return None
    
    # Criar cópia só com geometria
    gdf_limpo = gdf[['geometry']].copy()
    
    # Adicionar atributo 'nome' se existir (útil para popup)
    if 'nome' in gdf.columns:
        gdf_limpo['nome'] = gdf['nome'].astype(str)
    elif 'Name' in gdf.columns:
        gdf_limpo['nome'] = gdf['Name'].astype(str)
    
    return gdf_limpo


def calcular_area_ha(gdf):
    """Calcula área total em hectares reprojetando para UTM adequado à área"""
    centroid = gdf.geometry.unary_union.centroid
    utm_zone = int((centroid.x + 180) / 6) + 1
    epsg = 32600 + utm_zone if centroid.y >= 0 else 32700 + utm_zone
    return gdf.to_crs(epsg=epsg).geometry.area.sum() / 10000


def _geom_para_gee(geom):
    """Sanitiza geometria Shapely para aceite pelo GEE.
    1. Corrige invalidade via buffer(0)
    2. Aplica make_valid() se disponível (Shapely ≥ 1.8)
    3. Garante winding order CCW exterior (exigido pelo GeoJSON do GEE)
    4. Arredonda coordenadas para 7 casas decimais (~1 cm)
    """
    if not geom.is_valid:
        geom = geom.buffer(0)
    try:
        geom = shapely.make_valid(geom)
        # make_valid() pode retornar GeometryCollection — extrair só polígonos
        if geom.geom_type == 'GeometryCollection':
            polys = [g for g in geom.geoms if g.geom_type in ('Polygon', 'MultiPolygon')]
            geom = unary_union(polys) if polys else geom.buffer(0)
    except AttributeError:
        pass
    geom = shapely_orient(geom, sign=1.0)  # exterior CCW, interior CW

    def _round_ring(coords):
        return [(round(c[0], 7), round(c[1], 7)) for c in coords]

    geojson = dict(geom.__geo_interface__)
    if geojson['type'] == 'Polygon':
        geojson['coordinates'] = [_round_ring(ring) for ring in geojson['coordinates']]
    elif geojson['type'] == 'MultiPolygon':
        geojson['coordinates'] = [
            [_round_ring(ring) for ring in poly]
            for poly in geojson['coordinates']
        ]
    return ee.Geometry(geojson)


# Definir tipos de cobertura (usado em várias partes do código)
tipos_cobertura = {
    'Floresta': '#00FF00',        # Verde limão (bem visível)
    'Pastagem': '#FFFF00',        # Amarelo puro
    'Água': '#00FFFF',            # Ciano (azul claro)
    'Outra Vegetação': '#FF00FF', # MAGENTA (roxo forte) - BEM DIFERENTE!
    'Solo Exposto': '#FF8C00',    # Laranja forte
    'Queimada': '#FF0000',        # Vermelho puro
    'Agricultura': '#FFD700'      # Dourado
}

# Inicializar session_state
if 'gdf' not in st.session_state:
    st.session_state.gdf = None
if 'gdf_parcelas' not in st.session_state:
    st.session_state.gdf_parcelas = None
if 'roi' not in st.session_state:
    st.session_state.roi = None
if 'img_anterior' not in st.session_state:
    st.session_state.img_anterior = None
if 'img_posterior' not in st.session_state:
    st.session_state.img_posterior = None
if 'date_ant' not in st.session_state:
    st.session_state.date_ant = None
if 'date_pos' not in st.session_state:
    st.session_state.date_pos = None
if 'mostrar_mapas_rgb' not in st.session_state:
    st.session_state.mostrar_mapas_rgb = False
if 'amostras_anterior' not in st.session_state:
    st.session_state.amostras_anterior = {
        'Floresta': [],
        'Pastagem': [],
        'Água': [],
        'Outra Vegetação': [],
        'Solo Exposto': [],
        'Queimada': [],
        'Agricultura': []
    }
if 'amostras_posterior' not in st.session_state:
    st.session_state.amostras_posterior = {
        'Floresta': [],
        'Pastagem': [],
        'Água': [],
        'Outra Vegetação': [],
        'Solo Exposto': [],
        'Queimada': [],
        'Agricultura': []
    }
if 'tipo_selecionado' not in st.session_state:
    st.session_state.tipo_selecionado = 'Floresta'
if 'periodo_coleta' not in st.session_state:
    st.session_state.periodo_coleta = 'anterior'
if 'last_click_coords' not in st.session_state:
    st.session_state.last_click_coords = None
# mostrar_marcadores: sempre True (feedback visual obrigatório)
# mostrar_lotes: sempre False (performance)

# Header
st.title(f"🌳 DARC - Sistema de Análise de Desmatamento {APP_VERSION}")
st.subheader("Instituto Federal de Rondônia")

# Indicador simples de status
if 'gee_initialized' in st.session_state and st.session_state.gee_initialized:
    st.caption("✅ GEE conectado")

with st.expander("💡 Dicas"):
    st.markdown("""
    - ⚡ GEE conecta ao buscar imagens (~5-10s primeira vez)
    - 💾 Cache acelera carregamentos seguintes
    - 🔄 Use "Recarregar Imagens" se o mapa travar
    """)
st.markdown("---")

# ETAPA 1: UPLOAD DO SHAPEFILE
st.header("📂 1. Importar Projeto de Assentamento (PA)")
st.caption("📋 Passo 1: Carregue os arquivos do mapa do assentamento")

col1, col2 = st.columns(2)

with col1:
    uploaded_perimetro = st.file_uploader(
        "📁 Arquivo do Perímetro do PA",
        type=['zip', 'geojson', 'json'],
        help="Arquivo ZIP ou GeoJSON com o contorno externo (limite) do Projeto de Assentamento",
        key="upload_perimetro"
    )

with col2:
    uploaded_parcelas = st.file_uploader(
        "📁 Arquivo dos Lotes/Parcelas (opcional)",
        type=['zip', 'geojson', 'json'],
        help="Arquivo ZIP ou GeoJSON com os lotes individuais do assentamento. Se enviado sozinho, o perímetro é calculado automaticamente",
        key="upload_parcelas"
    )

st.info("💡 Dica: Você pode enviar apenas o arquivo de lotes — o perímetro será calculado automaticamente.")

# Processar uploads - prioridade: lotes (se existir sozinho) ou perímetro
if uploaded_parcelas is not None and uploaded_perimetro is None:
    # MODO: Só lotes - calcula perímetro automaticamente
    try:
        
        if uploaded_parcelas.name.endswith('.zip'):
            with tempfile.TemporaryDirectory() as tmpdir:
                zip_path = os.path.join(tmpdir, "parcelas.zip")
                with open(zip_path, "wb") as f:
                    f.write(uploaded_parcelas.getbuffer())
                
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(tmpdir)
                
                shp_files = [
                    os.path.join(dp, fname)
                    for dp, _, fnames in os.walk(tmpdir)
                    for fname in fnames if fname.endswith('.shp')
                ]
                if len(shp_files) > 0:
                    gdf_parcelas = gpd.read_file(shp_files[0])
                    if not gdf_parcelas.crs or not gdf_parcelas.crs.equals("EPSG:4326"):
                        gdf_parcelas = gdf_parcelas.to_crs("EPSG:4326")
                    st.session_state.gdf_parcelas = gdf_parcelas
                else:
                    st.error("❌ Nenhum arquivo .shp encontrado no ZIP dos lotes.")
                    st.info("💡 O ZIP deve conter os arquivos .shp, .dbf e .prj.")
                    st.stop()

        elif uploaded_parcelas.name.endswith(('.geojson', '.json')):
            geojson_data = json.loads(uploaded_parcelas.read())
            gdf_parcelas = gpd.GeoDataFrame.from_features(geojson_data['features'], crs="EPSG:4326")
            if not gdf_parcelas.crs or not gdf_parcelas.crs.equals("EPSG:4326"):
                gdf_parcelas = gdf_parcelas.to_crs("EPSG:4326")
            st.session_state.gdf_parcelas = gdf_parcelas
        
        # Calcular perímetro a partir dos lotes - SEM divisões internas
        
        # Unir todos os lotes com validação de geometrias
        try:
            # Garantir geometrias válidas
            st.session_state.gdf_parcelas['geometry'] = st.session_state.gdf_parcelas['geometry'].buffer(0)
            uniao = unary_union(st.session_state.gdf_parcelas.geometry).buffer(0)
        except Exception as e:
            st.error(f"❌ Erro ao processar geometrias: {e}")
            st.info("💡 Tente simplificar as geometrias ou usar outro arquivo.")
            st.stop()
        
        # Normalizar GeometryCollection → extrair só polígonos
        if uniao.geom_type == 'GeometryCollection':
            polys = [g for g in uniao.geoms if g.geom_type in ('Polygon', 'MultiPolygon')]
            if not polys:
                st.error("❌ Os lotes não contêm geometrias poligonais válidas.")
                st.stop()
            uniao = unary_union(polys)

        # Remover buracos (holes) e manter só contornos externos
        if uniao.geom_type == 'MultiPolygon':
            poligonos_limpos = [Polygon(poly.exterior.coords) for poly in uniao.geoms]
            perimetro_auto = MultiPolygon(poligonos_limpos)
        elif uniao.geom_type == 'Polygon':
            perimetro_auto = Polygon(uniao.exterior.coords)
        else:
            st.warning("⚠️ Geometria inesperada — usando união direta sem remover buracos internos.")
            perimetro_auto = uniao

        gdf_perimetro = gpd.GeoDataFrame({'nome': ['PA']}, geometry=[perimetro_auto], crs="EPSG:4326")
        st.session_state.gdf = gdf_perimetro
        st.session_state.roi = None  # Será criado apenas quando necessário

        num_areas = len(list(perimetro_auto.geoms)) if perimetro_auto.geom_type == 'MultiPolygon' else 1
        st.success(f"✅ {len(st.session_state.gdf_parcelas)} lotes carregados com sucesso!")
        st.success(f"✅ Perímetro calculado automaticamente a partir dos lotes ({num_areas} área(s)).")
        
        area_ha = calcular_area_ha(st.session_state.gdf)
        st.metric("📐 Área Total do PA", f"{area_ha:,.0f} ha")

    except Exception as e:
        st.error(f"❌ Erro ao processar lotes: {e}")
        st.code(traceback.format_exc())
        st.stop()

elif uploaded_perimetro is not None:
    with st.spinner("🔄 Processando perímetro..."):
        try:
            uploaded_file = uploaded_perimetro  # Usa o perímetro como principal
            if uploaded_file.name.endswith('.zip'):
                with tempfile.TemporaryDirectory() as tmpdir:
                    zip_path = os.path.join(tmpdir, "shapefile.zip")
                    with open(zip_path, "wb") as f:
                        f.write(uploaded_file.getbuffer())
                    
                    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                        zip_ref.extractall(tmpdir)
                    
                    shp_files = [
                        os.path.join(dp, fname)
                        for dp, _, fnames in os.walk(tmpdir)
                        for fname in fnames if fname.endswith('.shp')
                    ]
                    if len(shp_files) == 0:
                        st.error("❌ Nenhum arquivo .shp encontrado no .zip")
                        st.info("💡 O ZIP deve conter os arquivos .shp, .dbf e .prj.")
                        st.stop()

                    gdf = gpd.read_file(shp_files[0])
                    if not gdf.crs or not gdf.crs.equals("EPSG:4326"):
                        gdf = gdf.to_crs("EPSG:4326")
                    st.session_state.gdf = gdf
                    st.session_state.roi = None  # Será criado quando necessário

            elif uploaded_file.name.endswith(('.geojson', '.json')):
                geojson_data = json.loads(uploaded_file.read())
                gdf = gpd.GeoDataFrame.from_features(geojson_data['features'], crs="EPSG:4326")
                if not gdf.crs or not gdf.crs.equals("EPSG:4326"):
                    gdf = gdf.to_crs("EPSG:4326")
                
                st.session_state.gdf = gdf
                st.session_state.roi = None  # Será criado quando necessário
            
            st.success("✅ Arquivo carregado com sucesso!")
            
        except Exception as e:
            st.error(f"❌ Erro ao processar arquivo: {e}")
            st.stop()
        
        # Processar parcelas se foi enviado
        if uploaded_parcelas is not None:
            with st.spinner(f"🔄 Processando lotes..."):
                try:
                    if uploaded_parcelas.name.endswith('.zip'):
                        with tempfile.TemporaryDirectory() as tmpdir:
                            zip_path = os.path.join(tmpdir, "parcelas.zip")
                            with open(zip_path, "wb") as f:
                                f.write(uploaded_parcelas.getbuffer())
                            
                            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                                zip_ref.extractall(tmpdir)
                            
                            shp_files = [
                                os.path.join(dp, fname)
                                for dp, _, fnames in os.walk(tmpdir)
                                for fname in fnames if fname.endswith('.shp')
                            ]
                            if len(shp_files) > 0:
                                gdf_parcelas = gpd.read_file(shp_files[0])
                                if not gdf_parcelas.crs or not gdf_parcelas.crs.equals("EPSG:4326"):
                                    gdf_parcelas = gdf_parcelas.to_crs("EPSG:4326")
                                st.session_state.gdf_parcelas = gdf_parcelas
                                st.info(f"✅ Parcelas carregadas: {len(gdf_parcelas)} lotes")
                                
                    elif uploaded_parcelas.name.endswith(('.geojson', '.json')):
                        geojson_data = json.loads(uploaded_parcelas.read())
                        gdf_parcelas = gpd.GeoDataFrame.from_features(geojson_data['features'], crs="EPSG:4326")
                        if not gdf_parcelas.crs or not gdf_parcelas.crs.equals("EPSG:4326"):
                            gdf_parcelas = gdf_parcelas.to_crs("EPSG:4326")
                        st.session_state.gdf_parcelas = gdf_parcelas
                        st.info(f"✅ Parcelas carregadas: {len(gdf_parcelas)} lotes")
                except Exception as e:
                    st.warning(f"⚠️ Erro ao processar parcelas: {e}")
        
        area_ha = calcular_area_ha(st.session_state.gdf)
        st.metric("📐 Área Total do PA", f"{area_ha:,.0f} ha")

if st.session_state.gdf is not None:
    st.markdown("---")
    
    st.header("📅 2. Selecionar Período de Análise")
    st.caption("Escolha as duas datas que você quer comparar. A data anterior é a situação antiga (ex: 2008) e a posterior é a situação atual (ex: 2025).")
    
    col1, col2 = st.columns(2)
    with col1:
        data_anterior = st.date_input(
            "Data Anterior",
            value=date(2008, 7, 1),
            min_value=date(1984, 1, 1),
            max_value=date.today()
        )
    with col2:
        data_posterior = st.date_input(
            "Data Posterior (atual)",
            value=date(2025, 8, 1),
            min_value=date(1984, 1, 1),
            max_value=date.today()
        )
    
    if data_anterior >= data_posterior:
        st.warning("⚠️ A data anterior deve ser menor que a data posterior")
        st.stop()
    
    intervalo_anos = (data_posterior - data_anterior).days / 365.25
    st.info(f"📊 Intervalo de análise: {intervalo_anos:.1f} anos")
    
    cloud_cover = st.slider("☁️ Cobertura máxima de nuvens (%)", 0, 100, 50, 5)
    
    col_btn1, col_btn2 = st.columns([3, 1])
    with col_btn2:
        if st.button("🔄 Recarregar Imagens", help="Apaga as imagens carregadas e busca novamente"):
            # Limpar imagens
            if 'img_anterior' in st.session_state:
                del st.session_state.img_anterior
            if 'img_posterior' in st.session_state:
                del st.session_state.img_posterior
            # Limpar tiles
            if 'tile_url_ant' in st.session_state:
                del st.session_state.tile_url_ant
            if 'tile_url_pos' in st.session_state:
                del st.session_state.tile_url_pos
            if 'date_ant_cache' in st.session_state:
                del st.session_state.date_ant_cache
            if 'date_pos_cache' in st.session_state:
                del st.session_state.date_pos_cache
            # Limpar flag de mapas RGB
            if 'mostrar_mapas_rgb' in st.session_state:
                st.session_state.mostrar_mapas_rgb = False
            # Limpar spacecraft IDs cacheados
            if 'sat_ant_id' in st.session_state:
                del st.session_state.sat_ant_id
            if 'sat_pos_id' in st.session_state:
                del st.session_state.sat_pos_id
            # Limpar tiles do mapa de coleta (fragment)
            for _k in ['tile_url_coleta_anterior', 'tile_url_coleta_posterior']:
                if _k in st.session_state:
                    del st.session_state[_k]
            st.success("✅ Cache limpo!")
            st.rerun()
    
    st.markdown("---")
    
    st.header("🛰️ 3. Buscar Imagens de Satélite")
    st.caption("Clique no botão para baixar automaticamente as imagens de satélite Landsat dos dois períodos selecionados.")
    
    if st.button("🔍 Buscar Imagens", type="primary"):
        st.session_state.buscar_clicked = True
        st.rerun()
    
    if st.session_state.get('buscar_clicked', False):
        st.session_state.buscar_clicked = False
        # Inicializar GEE apenas agora (lazy loading)
        inicializar_gee()
        
        with st.spinner("Buscando imagens no Google Earth Engine..."):
            try:
                def apply_scale_factors(image):
                    optical = image.select('SR_B.').multiply(0.0000275).add(-0.2)
                    return image.addBands(optical, None, True)
                
                def buscar_imagem(start_date, roi, cloud_max):
                    # Determinar ano para escolher Landsat correto
                    year = int(start_date.split('-')[0])
                    
                    # Escolher coleção baseada no ano (igual GEE)
                    if year <= 2011:
                        collections = ['LANDSAT/LT05/C02/T1_L2']  # Landsat 5
                    elif year <= 2013:
                        collections = ['LANDSAT/LE07/C02/T1_L2', 'LANDSAT/LT05/C02/T1_L2']  # L7 ou L5
                    elif year <= 2021:
                        collections = ['LANDSAT/LC08/C02/T1_L2', 'LANDSAT/LE07/C02/T1_L2']  # L8 ou L7
                    else:
                        collections = ['LANDSAT/LC09/C02/T1_L2', 'LANDSAT/LC08/C02/T1_L2']  # L9 ou L8
                    
                    # Obter bounds do ROI
                    roi_bounds = roi.bounds().getInfo()['coordinates'][0]
                    roi_lons = [p[0] for p in roi_bounds]
                    roi_lats = [p[1] for p in roi_bounds]
                    roi_min_lon, roi_max_lon = min(roi_lons), max(roi_lons)
                    roi_min_lat, roi_max_lat = min(roi_lats), max(roi_lats)
                    
                    for col_name in collections:
                        collection = ee.ImageCollection(col_name) \
                            .filterBounds(roi) \
                            .filterDate(
                                ee.Date(start_date).advance(-6, 'month'),
                                ee.Date(start_date).advance(12, 'month')
                            ) \
                            .filter(ee.Filter.lt('CLOUD_COVER', cloud_max)) \
                            .sort('CLOUD_COVER')
                        
                        # Verificar cada imagem até encontrar uma que cubra completamente
                        size = collection.size().getInfo()
                        if size > 0:
                            col_list = collection.toList(size)
                            for i in range(min(size, 10)):  # Tentar até 10 imagens
                                img = ee.Image(col_list.get(i))
                                # Verificar cobertura real pelo footprint da cena (não bounding box)
                                if img.geometry().contains(roi, maxError=100).getInfo():
                                    return img.set('collection', col_name)
                    
                    # ── Fallback: mosaico de rows adjacentes (mesma data e path) ──
                    for col_name in collections:
                        collection = ee.ImageCollection(col_name) \
                            .filterBounds(roi) \
                            .filterDate(
                                ee.Date(start_date).advance(-6, 'month'),
                                ee.Date(start_date).advance(12, 'month')
                            ) \
                            .filter(ee.Filter.lt('CLOUD_COVER', cloud_max)) \
                            .sort('CLOUD_COVER')

                        # Uma única chamada ao servidor: retorna props + geometria de todas as imagens
                        col_info = collection.limit(20).getInfo()
                        features = col_info.get('features', [])
                        if len(features) < 2:
                            continue

                        imgs_info = []
                        for feature in features:
                            props = feature['properties']
                            geom = feature['geometry']
                            coords = (geom['coordinates'][0] if geom['type'] == 'Polygon'
                                      else [c for ring in geom['coordinates'] for c in ring[0]])
                            imgs_info.append({
                                'img': ee.Image(feature['id']),
                                'props': props,
                                'bounds': coords
                            })

                        # Agrupar por path + data (mesmo sobrevoo)
                        grupos = {}
                        for info in imgs_info:
                            chave = (info['props'].get('WRS_PATH'), info['props'].get('DATE_ACQUIRED'))
                            if chave not in grupos:
                                grupos[chave] = []
                            grupos[chave].append(info)

                        # Verificar pares com rows adjacentes
                        for chave, grupo in grupos.items():
                            if len(grupo) < 2:
                                continue
                            grupo.sort(key=lambda x: x['props'].get('WRS_ROW', 0))
                            for j in range(len(grupo) - 1):
                                i1, i2 = grupo[j], grupo[j + 1]
                                row1 = i1['props'].get('WRS_ROW', 0)
                                row2 = i2['props'].get('WRS_ROW', 0)
                                if abs(row1 - row2) != 1:
                                    continue
                                lons = [p[0] for p in i1['bounds']] + [p[0] for p in i2['bounds']]
                                lats = [p[1] for p in i1['bounds']] + [p[1] for p in i2['bounds']]
                                if (min(lons) <= roi_min_lon and max(lons) >= roi_max_lon and
                                        min(lats) <= roi_min_lat and max(lats) >= roi_max_lat):
                                    mosaic = ee.ImageCollection([i1['img'], i2['img']]).mosaic()
                                    mosaic = mosaic \
                                        .set('system:time_start', i1['props']['system:time_start']) \
                                        .set('SPACECRAFT_ID', i1['props']['SPACECRAFT_ID']) \
                                        .set('CLOUD_COVER', max(
                                            i1['props'].get('CLOUD_COVER', 0),
                                            i2['props'].get('CLOUD_COVER', 0)
                                        )) \
                                        .set('collection', col_name)
                                    return mosaic

                    return None

                st.write("🔍 Buscando imagens de satélite que cobrem completamente a área...")
                
                # Obter ROI (cria ee.Geometry apenas agora)
                roi = obter_roi()
                
                with st.spinner("Buscando imagem ANTERIOR..."):
                    img_ant = buscar_imagem(data_anterior.strftime('%Y-%m-%d'), roi, cloud_cover)
                
                with st.spinner("Buscando imagem POSTERIOR..."):
                    img_pos = buscar_imagem(data_posterior.strftime('%Y-%m-%d'), roi, cloud_cover)
                
                if img_ant is None:
                    st.error(f"❌ Nenhuma imagem encontrada para {data_anterior}")
                    st.stop()
                
                if img_pos is None:
                    st.error(f"❌ Nenhuma imagem encontrada para {data_posterior}")
                    st.stop()
                
                img_ant = apply_scale_factors(img_ant)
                img_pos = apply_scale_factors(img_pos)
                
                date_ant = img_ant.date().format('YYYY-MM-dd').getInfo()
                cloud_ant = img_ant.get('CLOUD_COVER').getInfo()
                sat_ant = img_ant.get('SPACECRAFT_ID').getInfo()
                
                date_pos = img_pos.date().format('YYYY-MM-dd').getInfo()
                cloud_pos = img_pos.get('CLOUD_COVER').getInfo()
                sat_pos = img_pos.get('SPACECRAFT_ID').getInfo()
                
                # DEBUG: Mostrar IDs das imagens
                with st.expander("🔍 Informações Técnicas"):
                    try:
                        id_ant = img_ant.id().getInfo()
                        id_pos = img_pos.id().getInfo()
                        
                        col_debug1, col_debug2 = st.columns(2)
                        with col_debug1:
                            st.write("**Imagem Anterior:**")
                            st.code(f"ID: {id_ant}")
                            st.code(f"Data: {date_ant}")
                            st.code(f"Satélite: {sat_ant}")
                        
                        with col_debug2:
                            st.write("**Imagem Posterior:**")
                            st.code(f"ID: {id_pos}")
                            st.code(f"Data: {date_pos}")
                            st.code(f"Satélite: {sat_pos}")
                        
                        if id_ant == id_pos:
                            st.error("⚠️ **PROBLEMA DETECTADO:** As duas imagens têm o MESMO ID!")
                            st.error("São a MESMA imagem sendo usada para os dois períodos!")
                        else:
                            st.success("✅ As imagens são diferentes (IDs únicos)")
                    except Exception as e:
                        st.warning(f"Não foi possível obter IDs: {e}")
                
                st.success("✅ Imagens encontradas!")
                
                col1, col2 = st.columns(2)
                with col1:
                    st.write("**📸 Imagem Anterior:**")
                    st.write(f"📅 Data: {date_ant}")
                    st.write(f"☁️ Nuvens: {cloud_ant:.1f}%")
                    st.write(f"🛰️ Satélite: {sat_ant}")
                
                with col2:
                    st.write("**📸 Imagem Posterior:**")
                    st.write(f"📅 Data: {date_pos}")
                    st.write(f"☁️ Nuvens: {cloud_pos:.1f}%")
                    st.write(f"🛰️ Satélite: {sat_pos}")
                
                # VERIFICAÇÃO CRÍTICA: Datas iguais
                if date_ant == date_pos:
                    st.error("🚨 ERRO: Mesmas datas selecionadas!")
                    st.info("💡 Selecione datas diferentes")
                    st.stop()
                
                # Aviso se datas muito próximas
                dias_diferenca = abs((datetime.strptime(date_pos, '%Y-%m-%d') - datetime.strptime(date_ant, '%Y-%m-%d')).days)
                if dias_diferenca < 30:
                    st.warning(f"⚠️ As imagens têm apenas {dias_diferenca} dias de diferença. Mudanças podem ser mínimas.")
                
                st.session_state.img_anterior = img_ant
                st.session_state.img_posterior = img_pos
                st.session_state.date_ant = date_ant
                st.session_state.date_pos = date_pos
                st.session_state.sat_ant_id = sat_ant
                st.session_state.sat_pos_id = sat_pos
                
                # Definir bandas de visualização baseado no satélite
                if 'LANDSAT_5' in sat_ant or 'LANDSAT_7' in sat_ant or 'LT05' in sat_ant or 'LE07' in sat_ant:
                    vis_params_ant = {
                        'bands': ['SR_B3', 'SR_B2', 'SR_B1'],
                        'min': 0.02,
                        'max': 0.35,
                        'gamma': 1.3
                    }
                else:
                    vis_params_ant = {
                        'bands': ['SR_B4', 'SR_B3', 'SR_B2'],
                        'min': 0.02,
                        'max': 0.35,
                        'gamma': 1.3
                    }
                
                if 'LANDSAT_5' in sat_pos or 'LANDSAT_7' in sat_pos or 'LT05' in sat_pos or 'LE07' in sat_pos:
                    vis_params_pos = {
                        'bands': ['SR_B3', 'SR_B2', 'SR_B1'],
                        'min': 0.02,
                        'max': 0.35,
                        'gamma': 1.3
                    }
                else:
                    vis_params_pos = {
                        'bands': ['SR_B4', 'SR_B3', 'SR_B2'],
                        'min': 0.02,
                        'max': 0.35,
                        'gamma': 1.3
                    }
                
                st.success("✅ Imagens carregadas")
                
                # Salvar flag para mostrar mapas
                st.session_state.mostrar_mapas_rgb = True
                
            except Exception as e:
                st.error(f"❌ Erro: {e}")
                st.code(traceback.format_exc())
    
    # VISUALIZAÇÃO DOS MAPAS RGB - FORA DO BOTÃO
    if st.session_state.img_anterior is not None and st.session_state.get('mostrar_mapas_rgb', False):
        st.write("### 🗺️ Imagens de Satélite")
        
        date_ant = st.session_state.date_ant
        date_pos = st.session_state.date_pos
        
        # Parâmetros de visualização
        sat_ant = st.session_state.get('sat_ant_id') or st.session_state.img_anterior.get('SPACECRAFT_ID').getInfo()
        sat_pos = st.session_state.get('sat_pos_id') or st.session_state.img_posterior.get('SPACECRAFT_ID').getInfo()
        
        if 'LANDSAT_5' in sat_ant or 'LT05' in sat_ant:
            vis_params_ant = {'bands': ['SR_B3', 'SR_B2', 'SR_B1'], 'min': 0.02, 'max': 0.35, 'gamma': 1.3}
        else:
            vis_params_ant = {'bands': ['SR_B4', 'SR_B3', 'SR_B2'], 'min': 0.02, 'max': 0.35, 'gamma': 1.3}
        
        if 'LANDSAT_5' in sat_pos or 'LT05' in sat_pos:
            vis_params_pos = {'bands': ['SR_B3', 'SR_B2', 'SR_B1'], 'min': 0.02, 'max': 0.35, 'gamma': 1.3}
        else:
            vis_params_pos = {'bands': ['SR_B4', 'SR_B3', 'SR_B2'], 'min': 0.02, 'max': 0.35, 'gamma': 1.3}
        
        # Criar tile layers
        tile_url_ant = st.session_state.img_anterior.resample('bilinear').getMapId({**vis_params_ant, 'bestEffort': True})
        tile_url_pos = st.session_state.img_posterior.resample('bilinear').getMapId({**vis_params_pos, 'bestEffort': True})
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.write(f"**Período Anterior ({date_ant})**")
            bounds = st.session_state.gdf.total_bounds
            center = [(bounds[1] + bounds[3])/2, (bounds[0] + bounds[2])/2]
            
            m_ant = folium.Map(location=center, zoom_start=12)
            folium.TileLayer(
                tiles=tile_url_ant['tile_fetcher'].url_format,
                attr='Google Earth Engine',
                name='Landsat Anterior',
                overlay=True,
                max_zoom=20
            ).add_to(m_ant)
            
            folium.GeoJson(
                limpar_gdf_para_folium(st.session_state.gdf),
                style_function=lambda x: {'fillColor': 'transparent', 'color': 'yellow', 'weight': 3}
            ).add_to(m_ant)
            
            if st.session_state.gdf_parcelas is not None:
                folium.GeoJson(
                    limpar_gdf_para_folium(st.session_state.gdf_parcelas),
                    style_function=lambda x: {'fillColor': '#FFD700', 'fillOpacity': 0.2, 'color': '#FFA500', 'weight': 1}
                ).add_to(m_ant)
            
            st_folium(m_ant, width=None, height=400, key="map_ant_rgb_view")
        
        with col2:
            st.write(f"**Período Posterior ({date_pos})**")
            m_pos = folium.Map(location=center, zoom_start=12)
            
            folium.TileLayer(
                tiles=tile_url_pos['tile_fetcher'].url_format,
                attr='Google Earth Engine',
                name='Landsat Posterior',
                overlay=True,
                max_zoom=20
            ).add_to(m_pos)
            
            folium.GeoJson(
                limpar_gdf_para_folium(st.session_state.gdf),
                style_function=lambda x: {'fillColor': 'transparent', 'color': 'yellow', 'weight': 3}
            ).add_to(m_pos)
            
            if st.session_state.gdf_parcelas is not None:
                folium.GeoJson(
                    limpar_gdf_para_folium(st.session_state.gdf_parcelas),
                    style_function=lambda x: {'fillColor': '#FFD700', 'fillOpacity': 0.2, 'color': '#FFA500', 'weight': 1}
                ).add_to(m_pos)
            
            st_folium(m_pos, width=None, height=400, key="map_pos_rgb_view")
        
        # Expander de DEBUG
        with st.expander("🔍 Informações Técnicas"):
            try:
                id_ant = st.session_state.img_anterior.id().getInfo()
                id_pos = st.session_state.img_posterior.id().getInfo()
                
                col_debug1, col_debug2 = st.columns(2)
                with col_debug1:
                    st.write("**Imagem Anterior:**")
                    st.code(f"ID: {id_ant}")
                    st.code(f"Data: {date_ant}")
                    st.code(f"Satélite: {sat_ant}")
                
                with col_debug2:
                    st.write("**Imagem Posterior:**")
                    st.code(f"ID: {id_pos}")
                    st.code(f"Data: {date_pos}")
                    st.code(f"Satélite: {sat_pos}")
                
                if id_ant == id_pos:
                    st.error("⚠️ **PROBLEMA DETECTADO:** As duas imagens têm o MESMO ID!")
                    st.error("São a MESMA imagem sendo usada para os dois períodos!")
                else:
                    st.success("✅ As imagens são diferentes (IDs únicos)")
            except Exception as e:
                st.warning(f"Não foi possível obter IDs: {e}")
        
        st.markdown("---")
    
    if st.session_state.img_anterior is not None:
        st.header("📍 4. Coletar Amostras de Treinamento")
        st.caption("Clique no mapa para marcar exemplos de cada tipo de vegetação (floresta, pastagem, etc.). O sistema aprende com esses exemplos para identificar o desmatamento.")
        
        total_ant = sum(len(v) for v in st.session_state.amostras_anterior.values())
        total_pos = sum(len(v) for v in st.session_state.amostras_posterior.values())
        
        if total_ant > 0 or total_pos > 0:
            st.success(f"✅ Amostras carregadas: {total_ant} (anterior) + {total_pos} (posterior)")
            
            with st.expander("📊 Ver detalhes das amostras"):
                col1, col2 = st.columns(2)
                with col1:
                    st.write("**Período Anterior:**")
                    for tipo, pontos in st.session_state.amostras_anterior.items():
                        if len(pontos) > 0:
                            st.write(f"- {tipo}: {len(pontos)} amostras")
                
                with col2:
                    st.write("**Período Posterior:**")
                    for tipo, pontos in st.session_state.amostras_posterior.items():
                        if len(pontos) > 0:
                            st.write(f"- {tipo}: {len(pontos)} amostras")
        
        st.write("---")
        
        # NOVA SEÇÃO: Importar amostras por texto
        st.write("### 📥 Importar Amostras de outro sistema")
        
        st.info("""
        **⚠️ IMPORTANTE: Você DEVE especificar o tipo antes das coordenadas!**
        
        **Tipos válidos:** Floresta, Pastagem, Água, Outra Vegetação, Solo Exposto, Queimada, Agricultura
        
        **Formato correto:**
        ```
        Floresta
        -61.9345, -9.1523
        -61.9512, -9.1678
        
        Pastagem
        -61.9934, -9.1289
        -62.0145, -9.1356
        ```
        
        **❌ ERRADO (tudo vai para "Floresta"):**
        ```
        -61.9345, -9.1523
        -61.9512, -9.1678
        -61.9934, -9.1289
        ```
        """)
        
        st.warning("🚨 Se não especificar o tipo, TODAS as coordenadas vão para 'Floresta' e você terá apenas 1 classe!")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.write("**Período Anterior:**")
            texto_amostras_ant = st.text_area(
                "Cole as coordenadas aqui:",
                height=200,
                key="texto_ant",
                placeholder="Floresta\n-61.93, -9.15\n-61.95, -9.16\n\nPastagem\n-61.99, -9.12"
            )
            
            if st.button("📍 Importar Amostras Anterior", use_container_width=True):
                try:
                    # Processar texto
                    linhas = texto_amostras_ant.strip().split('\n')
                    tipo_atual = 'Floresta'  # padrão
                    amostras_temp = {k: [] for k in st.session_state.amostras_anterior.keys()}
                    
                    linhas_processadas = 0
                    erros = []
                    
                    for i, linha in enumerate(linhas, 1):
                        linha = linha.strip()
                        if not linha:
                            continue
                        
                        # Verificar se é um tipo de cobertura
                        if linha in amostras_temp.keys():
                            tipo_atual = linha
                            st.caption(f"Mudou para tipo: {tipo_atual}")
                        else:
                            # Tentar extrair coordenadas
                            # Suporta: "-61.93, -9.15" ou "[-61.93, -9.15]" ou "ee.Geometry.Point([-61.93, -9.15])"
                            coords = re.findall(r'-?\d+\.?\d*', linha)
                            if len(coords) >= 2:
                                lon = float(coords[0])
                                lat = float(coords[1])
                                
                                # Validar coordenadas (Brasil: lon -75 a -30, lat -35 a 5)
                                if -75 <= lon <= -30 and -35 <= lat <= 5:
                                    amostras_temp[tipo_atual].append([lon, lat])
                                    linhas_processadas += 1
                                else:
                                    erros.append(f"Linha {i}: Coordenadas fora do Brasil ({lon}, {lat})")
                            else:
                                erros.append(f"Linha {i}: Não encontrou 2 números ({linha})")
                    
                    # Mesclar com amostras existentes (manuais não são perdidas)
                    for tipo, novos_pontos in amostras_temp.items():
                        st.session_state.amostras_anterior[tipo].extend(novos_pontos)
                    total = sum(len(v) for v in amostras_temp.values())
                    
                    # Feedback detalhado
                    st.success(f"✅ {total} amostras importadas para período anterior!")
                    
                    # Mostrar distribuição
                    for tipo, pontos in amostras_temp.items():
                        if len(pontos) > 0:
                            st.info(f"  {tipo}: {len(pontos)} amostras")
                    
                    if erros:
                        with st.expander(f"⚠️ {len(erros)} linhas ignoradas"):
                            for erro in erros[:10]:  # Mostrar até 10
                                st.caption(erro)
                    
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"❌ Erro ao importar: {e}")
                    st.code(traceback.format_exc())
        
        with col2:
            st.write("**Período Posterior:**")
            texto_amostras_pos = st.text_area(
                "Cole as coordenadas aqui:",
                height=200,
                key="texto_pos",
                placeholder="Floresta\n-61.93, -9.15\n-61.95, -9.16\n\nPastagem\n-61.99, -9.12"
            )
            
            if st.button("📍 Importar Amostras Posterior", use_container_width=True):
                try:
                    # Processar texto
                    linhas = texto_amostras_pos.strip().split('\n')
                    tipo_atual = 'Floresta'
                    amostras_temp = {k: [] for k in st.session_state.amostras_posterior.keys()}
                    
                    linhas_processadas = 0
                    erros = []
                    
                    for i, linha in enumerate(linhas, 1):
                        linha = linha.strip()
                        if not linha:
                            continue
                        
                        if linha in amostras_temp.keys():
                            tipo_atual = linha
                            st.caption(f"Mudou para tipo: {tipo_atual}")
                        else:
                            coords = re.findall(r'-?\d+\.?\d*', linha)
                            if len(coords) >= 2:
                                lon = float(coords[0])
                                lat = float(coords[1])
                                
                                # Validar coordenadas (Brasil: lon -75 a -30, lat -35 a 5)
                                if -75 <= lon <= -30 and -35 <= lat <= 5:
                                    amostras_temp[tipo_atual].append([lon, lat])
                                    linhas_processadas += 1
                                else:
                                    erros.append(f"Linha {i}: Coordenadas fora do Brasil ({lon}, {lat})")
                            else:
                                erros.append(f"Linha {i}: Não encontrou 2 números ({linha})")
                    
                    # Mesclar com amostras existentes (manuais não são perdidas)
                    for tipo, novos_pontos in amostras_temp.items():
                        st.session_state.amostras_posterior[tipo].extend(novos_pontos)
                    total = sum(len(v) for v in amostras_temp.values())

                    # Feedback detalhado
                    st.success(f"✅ {total} amostras importadas para período posterior!")
                    
                    # Mostrar distribuição
                    for tipo, pontos in amostras_temp.items():
                        if len(pontos) > 0:
                            st.info(f"  {tipo}: {len(pontos)} amostras")
                    
                    if erros:
                        with st.expander(f"⚠️ {len(erros)} linhas ignoradas"):
                            for erro in erros[:10]:
                                st.caption(erro)
                    
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"❌ Erro ao importar: {e}")
                    st.code(traceback.format_exc())
        
        st.write("---")
        st.write("### 🎯 Coletar Amostras Manualmente (clicando no mapa)")

        @st.fragment
        def mapa_coleta_fragmento():
            periodo_coleta = st.session_state.periodo_coleta
            tipo_selecionado = st.session_state.tipo_selecionado
            periodo_label = "Anterior" if periodo_coleta == 'anterior' else "Posterior"
            amostras = (st.session_state.amostras_anterior
                        if periodo_coleta == 'anterior'
                        else st.session_state.amostras_posterior)

            st.write(f"#### 🗺️ Mapa - Período {periodo_label}")

            if periodo_coleta == 'anterior':
                data_exibida = st.session_state.date_ant
                if 'sat_ant_id' not in st.session_state:
                    st.session_state.sat_ant_id = (
                        st.session_state.img_anterior.get('SPACECRAFT_ID').getInfo())
                sat_exibido = st.session_state.sat_ant_id
                img_para_mapa = st.session_state.img_anterior
                data_mapa = st.session_state.date_ant
            else:
                data_exibida = st.session_state.date_pos
                if 'sat_pos_id' not in st.session_state:
                    st.session_state.sat_pos_id = (
                        st.session_state.img_posterior.get('SPACECRAFT_ID').getInfo())
                sat_exibido = st.session_state.sat_pos_id
                img_para_mapa = st.session_state.img_posterior
                data_mapa = st.session_state.date_pos

            st.info(f"📸 **{data_exibida}** ({sat_exibido})")

            n_amostras_tipo = len(amostras[tipo_selecionado])
            st.metric(f"📍 {tipo_selecionado}", f"{n_amostras_tipo} amostras")

            bounds = st.session_state.gdf.total_bounds
            center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]
            if 'map_zoom' not in st.session_state:
                st.session_state.map_zoom = 13

            m = folium.Map(
                location=center,
                zoom_start=st.session_state.map_zoom,
                zoom_control=True,
                scrollWheelZoom=True,
                prefer_canvas=True
            )

            # Cache tile URL por período — evita GEE call no rerun do fragmento
            tile_cache_key = f'tile_url_coleta_{periodo_coleta}'
            if tile_cache_key not in st.session_state:
                if 'LANDSAT_5' in sat_exibido or 'LT05' in sat_exibido:
                    vis_params = {
                        'bands': ['SR_B3', 'SR_B2', 'SR_B1'],
                        'min': 0.02, 'max': 0.35, 'gamma': 1.3
                    }
                else:
                    vis_params = {
                        'bands': ['SR_B4', 'SR_B3', 'SR_B2'],
                        'min': 0.02, 'max': 0.35, 'gamma': 1.3
                    }
                tile_info = img_para_mapa.resample('bilinear').getMapId(
                    {**vis_params, 'bestEffort': True})
                st.session_state[tile_cache_key] = tile_info['tile_fetcher'].url_format

            folium.TileLayer(
                tiles=st.session_state[tile_cache_key],
                attr='Google Earth Engine',
                name=f'Landsat {data_mapa}',
                overlay=True,
                max_zoom=20
            ).add_to(m)

            folium.GeoJson(
                limpar_gdf_para_folium(st.session_state.gdf),
                style_function=lambda x: {
                    'fillColor': 'transparent', 'color': '#00FF00', 'weight': 2
                }
            ).add_to(m)

            if (st.session_state.gdf_parcelas is not None
                    and st.session_state.get('mostrar_lotes', False)):
                folium.GeoJson(
                    limpar_gdf_para_folium(st.session_state.gdf_parcelas),
                    style_function=lambda x: {
                        'fillColor': '#FFD700', 'fillOpacity': 0.15,
                        'color': '#FFA500', 'weight': 1
                    }
                ).add_to(m)

            MAX_MARCADORES = 200
            total_marcadores = sum(len(pontos) for pontos in amostras.values())
            if total_marcadores > MAX_MARCADORES:
                st.caption(f"⚡ Exibindo primeiros {MAX_MARCADORES} de {total_marcadores} marcadores")

            marcadores_adicionados = 0
            for tipo, pontos in amostras.items():
                if len(pontos) > 0:
                    cor = tipos_cobertura[tipo]
                    for ponto in pontos:
                        if marcadores_adicionados >= MAX_MARCADORES:
                            break
                        folium.CircleMarker(
                            location=[ponto[1], ponto[0]],
                            radius=4,
                            color='white',
                            weight=1,
                            fill=True,
                            fillColor=cor,
                            fillOpacity=0.8
                        ).add_to(m)
                        marcadores_adicionados += 1

            map_data = st_folium(
                m,
                width=1000,
                height=450,
                key=f"map_coleta_{periodo_coleta}",
                returned_objects=["last_clicked"]
            )

            if map_data and map_data.get('last_clicked'):
                lat = map_data['last_clicked']['lat']
                lon = map_data['last_clicked']['lng']
                click_atual = (lat, lon)

                if click_atual != st.session_state.last_click_coords:
                    st.session_state.last_click_coords = click_atual

                    novo_ponto = [lon, lat]
                    if periodo_coleta == 'anterior':
                        st.session_state.amostras_anterior[tipo_selecionado].append(novo_ponto)
                    else:
                        st.session_state.amostras_posterior[tipo_selecionado].append(novo_ponto)

                    total_atual = len(amostras[tipo_selecionado])
                    st.toast(f"✅ {tipo_selecionado} #{total_atual} coletada!", icon="📍")
                    st.rerun()  # reruna só o fragmento

        col1, col2 = st.columns([1, 3])
        
        with col1:
            st.write("#### 🎨 Tipo de Cobertura")
            
            # Radio ao invés de botões (mais rápido, sem rerun)
            tipo_opcoes = list(tipos_cobertura.keys())
            tipo_index = tipo_opcoes.index(st.session_state.tipo_selecionado) if st.session_state.tipo_selecionado in tipo_opcoes else 0
            
            tipo_selecionado = st.radio(
                "Selecione o tipo:",
                tipo_opcoes,
                index=tipo_index,
                key='tipo_radio',
                label_visibility="collapsed"
            )
            st.session_state.tipo_selecionado = tipo_selecionado
            
            # Mostrar cor do tipo selecionado
            cor_atual = tipos_cobertura[tipo_selecionado]
            st.markdown(f"**Cor:** <span style='font-size:24px; color:{cor_atual}'>●</span>", unsafe_allow_html=True)
            
            st.write("#### 📅 Período")
            periodo = st.radio(
                "Coletar amostras para:",
                ['Anterior', 'Posterior'],
                key='periodo_radio',
                help="Selecione o período e o mapa mostrará a imagem Landsat correspondente"
            )
            
            # Detectar mudança de período
            periodo_novo = periodo.lower()
            if 'periodo_coleta' in st.session_state and st.session_state.periodo_coleta != periodo_novo:
                st.success(f"✅ Mapa atualizado para {periodo}!")
            
            st.session_state.periodo_coleta = periodo_novo
            
            st.write("#### 📊 Resumo de Amostras")
            periodo_label = "Anterior" if st.session_state.periodo_coleta == 'anterior' else "Posterior"
            amostras = st.session_state.amostras_anterior if st.session_state.periodo_coleta == 'anterior' else st.session_state.amostras_posterior
            
            # Opção para mostrar lotes no mapa (MANTIDO)
            if st.session_state.gdf_parcelas is not None:
                col_check, col_info = st.columns([3, 1])
                
                with col_check:
                    mostrar = st.checkbox(
                        "🗺️ Mostrar lotes no mapa",
                        value=False,  # Desmarcado por padrão (performance)
                        help=f"Exibe os {len(st.session_state.gdf_parcelas)} lotes no mapa",
                        key=f"checkbox_lotes_{st.session_state.periodo_coleta}"
                    )
                    st.session_state.mostrar_lotes = mostrar
                
                with col_info:
                    if mostrar:
                        st.info(f"🗺️ {len(st.session_state.gdf_parcelas)} lotes")
                    else:
                        st.success("⚡ Carregamento rápido")
            
            st.write(f"**Período {periodo_label}:**")
            total = 0
            classes_com_dados = 0
            for tipo, pontos in amostras.items():
                n = len(pontos)
                total += n
                if n > 0:
                    classes_com_dados += 1
                    emoji = "✅" if n >= 10 else "⚠️" if n >= 5 else "❌"
                    st.write(f"{emoji} {tipo}: {n}")
            
            col_a, col_b = st.columns(2)
            with col_a:
                st.metric("Total", total)
            with col_b:
                cor_classes = "🟢" if classes_com_dados >= 2 else "🔴"
                st.metric("Classes", f"{cor_classes} {classes_com_dados}")
            
            # Aviso se classes insuficientes
            if classes_com_dados < 2:
                st.error(f"⚠️ {classes_com_dados} classe(s) - MÍNIMO: 2 classes!")
            elif classes_com_dados == 2:
                st.success("✅ 2 classes - OK para processar!")
            else:
                st.success(f"✅ {classes_com_dados} classes - Ótimo!")
            
            if st.button("🗑️ Limpar Amostras", use_container_width=True):
                if st.session_state.periodo_coleta == 'anterior':
                    for tipo in st.session_state.amostras_anterior.keys():
                        st.session_state.amostras_anterior[tipo] = []
                    st.success("✅ Amostras do período anterior limpas!")
                else:
                    for tipo in st.session_state.amostras_posterior.keys():
                        st.session_state.amostras_posterior[tipo] = []
                    st.success("✅ Amostras do período posterior limpas!")
        
        with col2:
            mapa_coleta_fragmento()
        
        st.markdown("---")
        
        # DEBUG: Mostrar distribuição real das amostras
        with st.expander("🔍 Ver detalhes das amostras"):
            st.write("**Período Anterior:**")
            for tipo in tipos_cobertura.keys():
                n = len(st.session_state.amostras_anterior[tipo])
                if n > 0:
                    st.success(f"✅ {tipo}: {n} amostras")
                    st.caption(f"   Primeiras: {st.session_state.amostras_anterior[tipo][:3]}")
                else:
                    st.error(f"❌ {tipo}: 0 amostras")

            st.write("**Período Posterior:**")
            for tipo in tipos_cobertura.keys():
                n = len(st.session_state.amostras_posterior[tipo])
                if n > 0:
                    st.success(f"✅ {tipo}: {n} amostras")
                    st.caption(f"   Primeiras: {st.session_state.amostras_posterior[tipo][:3]}")
                else:
                    st.error(f"❌ {tipo}: 0 amostras")
        
        st.header("⚙️ 5. Processar Análise")
        st.caption("Quando terminar de marcar os exemplos nos dois períodos, clique no botão abaixo para iniciar a análise. O processo leva alguns minutos.")
        
        avisos = []
        
        if total_ant == 0:
            avisos.append("❌ Nenhuma amostra coletada para o período anterior!")
        if total_pos == 0:
            avisos.append("❌ Nenhuma amostra coletada para o período posterior!")
        
        for tipo in tipos_cobertura.keys():
            n_ant = len(st.session_state.amostras_anterior[tipo])
            n_pos = len(st.session_state.amostras_posterior[tipo])
            
            if n_ant > 0 and n_ant < 5:
                avisos.append(f"⚠️ '{tipo}' (Anterior): apenas {n_ant} amostras.")
            if n_pos > 0 and n_pos < 5:
                avisos.append(f"⚠️ '{tipo}' (Posterior): apenas {n_pos} amostras.")
        
        # Verificar número de classes com amostras
        classes_com_amostras_ant = sum(1 for tipo in tipos_cobertura.keys() if len(st.session_state.amostras_anterior[tipo]) > 0)
        classes_com_amostras_pos = sum(1 for tipo in tipos_cobertura.keys() if len(st.session_state.amostras_posterior[tipo]) > 0)
        
        # VALIDAÇÃO CRÍTICA: Precisa de pelo menos 2 classes
        if classes_com_amostras_ant < 2:
            st.error(f"🚨 PERÍODO ANTERIOR: {classes_com_amostras_ant} classe(s) coletada(s)")
            st.error("❌ INSUFICIENTE! Mínimo necessário: 2 classes")
            st.info("✅ Exemplo válido: Floresta (10 amostras) + Pastagem (10 amostras)")
            avisos.append("❌ BLOQUEADO: Período anterior precisa de 2+ classes")
        
        if classes_com_amostras_pos < 2:
            st.error(f"🚨 PERÍODO POSTERIOR: {classes_com_amostras_pos} classe(s) coletada(s)")
            st.error("❌ INSUFICIENTE! Mínimo necessário: 2 classes")
            st.info("✅ Exemplo válido: Floresta (10 amostras) + Pastagem (10 amostras)")
            avisos.append("❌ BLOQUEADO: Período posterior precisa de 2+ classes")
        
        if avisos:
            st.warning('\n\n'.join(avisos))
            if classes_com_amostras_ant >= 2 and classes_com_amostras_pos >= 2:
                st.info("💡 Você pode continuar, mas a acurácia pode ser menor.")
        
        # Desabilitar botão se não tiver classes suficientes
        botao_desabilitado = classes_com_amostras_ant < 2 or classes_com_amostras_pos < 2
        
        if st.button("🚀 Iniciar Análise de Desmatamento", type="primary", disabled=botao_desabilitado):
            # ===== VERIFICAÇÃO FAILSAFE (dupla segurança) =====
            classes_ant_final = sum(1 for tipo in tipos_cobertura.keys() if len(st.session_state.amostras_anterior[tipo]) > 0)
            classes_pos_final = sum(1 for tipo in tipos_cobertura.keys() if len(st.session_state.amostras_posterior[tipo]) > 0)
            
            if classes_ant_final < 2:
                st.error(f"🚨 BLOQUEADO: Período Anterior tem {classes_ant_final} classe(s)")
                st.error("🎯 Colete amostras de PELO MENOS 2 TIPOS diferentes!")
                st.error(f"Exemplo: Floresta + Pastagem")
                st.stop()
            
            if classes_pos_final < 2:
                st.error(f"🚨 BLOQUEADO: Período Posterior tem {classes_pos_final} classe(s)")
                st.error("🎯 Colete amostras de PELO MENOS 2 TIPOS diferentes!")
                st.error(f"Exemplo: Floresta + Pastagem")
                st.stop()
            
            # Inicializar GEE se ainda não foi inicializado
            inicializar_gee()
            
            # CRÍTICO: Obter ROI antes de processar
            roi = obter_roi()
            if roi is None:
                st.error("❌ Erro: ROI não pôde ser criado. Reimporte o perímetro.")
                st.stop()
            
            with st.spinner("Processando... Isso pode levar alguns minutos..."):
                try:
                    def preparar_bandas(image, is_l5=False):
                        if is_l5:
                            bands = image.select(['SR_B1', 'SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B7'])
                            ndvi = image.normalizedDifference(['SR_B4', 'SR_B3']).rename('NDVI')
                            savi = image.expression(
                                '(((NIR - RED) / (NIR + RED + 0.5))*(1+0.5))',
                                {'NIR': image.select('SR_B4'), 'RED': image.select('SR_B3')}
                            ).rename('SAVI')
                            nbr = image.normalizedDifference(['SR_B4', 'SR_B7']).rename('NBR')
                            mndwi = image.normalizedDifference(['SR_B2', 'SR_B5']).rename('MNDWI')
                        else:
                            bands = image.select(['SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B6', 'SR_B7'])
                            ndvi = image.normalizedDifference(['SR_B5', 'SR_B4']).rename('NDVI')
                            savi = image.expression(
                                '(((NIR - RED) / (NIR + RED + 0.5))*(1+0.5))',
                                {'NIR': image.select('SR_B5'), 'RED': image.select('SR_B4')}
                            ).rename('SAVI')
                            nbr = image.normalizedDifference(['SR_B5', 'SR_B7']).rename('NBR')
                            mndwi = image.normalizedDifference(['SR_B3', 'SR_B6']).rename('MNDWI')
                        
                        return bands.addBands(ndvi).addBands(savi).addBands(nbr).addBands(mndwi)
                    
                    sat_ant = st.session_state.get('sat_ant_id') or st.session_state.img_anterior.get('SPACECRAFT_ID').getInfo()
                    sat_pos = st.session_state.get('sat_pos_id') or st.session_state.img_posterior.get('SPACECRAFT_ID').getInfo()

                    is_l5_ant = 'LANDSAT_5' in sat_ant or 'LT05' in sat_ant
                    is_l5_pos = 'LANDSAT_5' in sat_pos or 'LT05' in sat_pos
                    
                    bands_ant = preparar_bandas(st.session_state.img_anterior, is_l5_ant)
                    bands_pos = preparar_bandas(st.session_state.img_posterior, is_l5_pos)
                    
                    def criar_samples(amostras_dict, class_map):
                        features = []
                        for tipo, pontos in amostras_dict.items():
                            if len(pontos) > 0:
                                class_num = class_map[tipo]
                                for ponto in pontos:
                                    features.append(ee.Feature(
                                        ee.Geometry.Point(ponto),
                                        {'class': class_num}
                                    ))
                        return ee.FeatureCollection(features)
                    
                    class_map = {tipo: i for i, tipo in enumerate(tipos_cobertura.keys())}
                    
                    samples_ant_all = criar_samples(st.session_state.amostras_anterior, class_map)
                    samples_pos_all = criar_samples(st.session_state.amostras_posterior, class_map)
                    
                    n_samples_ant = samples_ant_all.size().getInfo()
                    n_samples_pos = samples_pos_all.size().getInfo()
                    
                    st.info(f"📊 Total de amostras: Anterior={n_samples_ant}, Posterior={n_samples_pos}")

                    # Split adaptativo baseado na menor classe de cada período
                    min_ant = min((len(v) for v in st.session_state.amostras_anterior.values() if v), default=0)
                    min_pos = min((len(v) for v in st.session_state.amostras_posterior.values() if v), default=0)

                    if min_ant < 6:
                        split_ant = None
                        st.warning("⚠️ Período Anterior: alguma classe tem menos de 6 amostras. Usando todos os pontos para treino — indicadores de acurácia não serão calculados.")
                    elif min_ant < 10:
                        split_ant = 0.8
                        st.info("ℹ️ Período Anterior: poucas amostras — usando divisão 80/20 para maximizar o treino.")
                    else:
                        split_ant = 0.7

                    if min_pos < 6:
                        split_pos = None
                        st.warning("⚠️ Período Posterior: alguma classe tem menos de 6 amostras. Usando todos os pontos para treino — indicadores de acurácia não serão calculados.")
                    elif min_pos < 10:
                        split_pos = 0.8
                        st.info("ℹ️ Período Posterior: poucas amostras — usando divisão 80/20 para maximizar o treino.")
                    else:
                        split_pos = 0.7

                    # Split treino/validação com randomColumn
                    if split_ant is not None:
                        samples_ant_all = samples_ant_all.randomColumn('random', seed=0)
                        training_ant   = samples_ant_all.filter(ee.Filter.lt('random', split_ant))
                        validation_ant = samples_ant_all.filter(ee.Filter.gte('random', split_ant))
                    else:
                        training_ant   = samples_ant_all
                        validation_ant = None

                    if split_pos is not None:
                        samples_pos_all = samples_pos_all.randomColumn('random', seed=42)
                        training_pos   = samples_pos_all.filter(ee.Filter.lt('random', split_pos))
                        validation_pos = samples_pos_all.filter(ee.Filter.gte('random', split_pos))
                    else:
                        training_pos   = samples_pos_all
                        validation_pos = None

                    # Verificar tamanho da validação antes de prosseguir
                    if validation_ant is not None:
                        n_val_ant = validation_ant.size().getInfo()
                        if n_val_ant == 0:
                            st.error("❌ Validação ANTERIOR vazia após split! Colete mais amostras (mínimo 10 por classe).")
                            st.stop()
                    if validation_pos is not None:
                        n_val_pos = validation_pos.size().getInfo()
                        if n_val_pos == 0:
                            st.error("❌ Validação POSTERIOR vazia após split! Colete mais amostras (mínimo 10 por classe).")
                            st.stop()

                    # Buffer 30m por ponto de treino → captura ~5 pixels por amostra (replica GEE original)
                    training_ant_buf = training_ant.map(lambda f: f.buffer(30))
                    training_data_ant = bands_ant.sampleRegions(
                        collection=training_ant_buf,
                        properties=['class'],
                        scale=30
                    )
                    
                    # Verificar amostras extraídas
                    n_training_ant = training_data_ant.size().getInfo()
                    
                    if n_training_ant == 0:
                        st.error("❌ Amostras ANTERIOR fora da imagem!")
                        st.stop()
                    
                    # VALIDAÇÃO CRÍTICA: Verificar classes únicas
                    classes_unicas_ant = training_data_ant.aggregate_array('class').distinct().size().getInfo()
                    st.info(f"🎯 Período ANTERIOR: {n_training_ant} amostras, {classes_unicas_ant} classes")
                    
                    if classes_unicas_ant < 2:
                        st.error(f"❌ ERRO: Apenas {classes_unicas_ant} classe no período ANTERIOR!")
                        st.error("💡 Colete amostras de pelo menos 2 tipos diferentes")
                        st.stop()
                    
                    classifier_ant = ee.Classifier.smileRandomForest(
                        numberOfTrees=50,
                        minLeafPopulation=5,
                        bagFraction=0.5
                    ).train(
                        features=training_data_ant,
                        classProperty='class',
                        inputProperties=bands_ant.bandNames()
                    )
                    
                    training_pos_buf = training_pos.map(lambda f: f.buffer(30))
                    training_data_pos = bands_pos.sampleRegions(
                        collection=training_pos_buf,
                        properties=['class'],
                        scale=30
                    )
                    
                    # Verificar amostras extraídas
                    n_training_pos = training_data_pos.size().getInfo()
                    
                    if n_training_pos == 0:
                        st.error("❌ Amostras POSTERIOR fora da imagem!")
                        st.stop()
                    
                    # VALIDAÇÃO CRÍTICA: Verificar classes únicas
                    classes_unicas_pos = training_data_pos.aggregate_array('class').distinct().size().getInfo()
                    st.info(f"🎯 Período POSTERIOR: {n_training_pos} amostras, {classes_unicas_pos} classes")
                    
                    if classes_unicas_pos < 2:
                        st.error(f"❌ ERRO: Apenas {classes_unicas_pos} classe no período POSTERIOR!")
                        st.error("💡 Colete amostras de pelo menos 2 tipos diferentes")
                        st.stop()
                    
                    classifier_pos = ee.Classifier.smileRandomForest(
                        numberOfTrees=50,
                        minLeafPopulation=5,
                        bagFraction=0.5
                    ).train(
                        features=training_data_pos,
                        classProperty='class',
                        inputProperties=bands_pos.bandNames()
                    )
                    
                    classified_ant = bands_ant.classify(classifier_ant).clip(st.session_state.roi)
                    classified_pos = bands_pos.classify(classifier_pos).clip(st.session_state.roi)
                    
                    st.info("📊 Calculando acurácia...")

                    # Validação ANTERIOR
                    if validation_ant is not None:
                        test_ant = classified_ant.sampleRegions(
                            collection=validation_ant,
                            properties=['class'],
                            scale=30
                        )
                        confusion_ant = test_ant.errorMatrix('class', 'classification')
                        accuracy_ant = confusion_ant.accuracy().getInfo()
                        kappa_ant = confusion_ant.kappa().getInfo()
                        matrix_ant = confusion_ant.getInfo()
                    else:
                        accuracy_ant = kappa_ant = matrix_ant = None

                    # Validação POSTERIOR
                    if validation_pos is not None:
                        test_pos = classified_pos.sampleRegions(
                            collection=validation_pos,
                            properties=['class'],
                            scale=30
                        )
                        confusion_pos = test_pos.errorMatrix('class', 'classification')
                        accuracy_pos = confusion_pos.accuracy().getInfo()
                        kappa_pos = confusion_pos.kappa().getInfo()
                        matrix_pos = confusion_pos.getInfo()
                    else:
                        accuracy_pos = kappa_pos = matrix_pos = None
                    
                    # Nomes de classes como dict {índice: nome} — robusto contra gaps na sequência
                    class_names_ant = {class_map[t]: t for t in tipos_cobertura.keys()
                                       if len(st.session_state.amostras_anterior[t]) > 0}
                    class_names_pos = {class_map[t]: t for t in tipos_cobertura.keys()
                                       if len(st.session_state.amostras_posterior[t]) > 0}

                    st.session_state['classified_ant'] = classified_ant
                    st.session_state['classified_pos'] = classified_pos
                    st.session_state['accuracy_ant'] = accuracy_ant
                    st.session_state['kappa_ant'] = kappa_ant
                    st.session_state['matrix_ant'] = matrix_ant
                    st.session_state['class_names_ant'] = class_names_ant
                    st.session_state['accuracy_pos'] = accuracy_pos
                    st.session_state['kappa_pos'] = kappa_pos
                    st.session_state['matrix_pos'] = matrix_pos
                    st.session_state['class_names_pos'] = class_names_pos
                    
                    st.success("✅ Classificação concluída! Role para baixo!")
                    
                except Exception as e:
                    st.error(f"❌ Erro durante processamento: {e}")
                    st.code(traceback.format_exc())
        
        if 'classified_ant' in st.session_state:
            st.markdown("---")
            st.header("📊 6. Resultados da Análise")
            
            st.subheader("🎯 Acurácia da Classificação")
            
            col1, col2 = st.columns(2)
            
            def _kappa_label(k):
                if k >= 0.80: return "Excelente ✅"
                elif k >= 0.60: return "Bom ✅"
                elif k >= 0.40: return "Moderado ⚠️"
                else: return "Ruim ❌"

            with col1:
                st.write("**📅 Período Anterior (2008)**")
                accuracy_ant = st.session_state.get('accuracy_ant')
                kappa_ant = st.session_state.get('kappa_ant')
                if accuracy_ant is not None:
                    st.metric("Precisão Global", f"{accuracy_ant*100:.2f}%")
                    st.metric("Índice Kappa", f"{kappa_ant:.4f} — {_kappa_label(kappa_ant)}")
                    st.write("**Matriz de Confusão:**")
                    st.caption("Quanto mais números na diagonal (canto superior esquerdo → inferior direito), melhor a classificação.")
                    matrix_ant = st.session_state.get('matrix_ant', [[0]])
                    n_cols_ant = len(matrix_ant[0]) if matrix_ant else 0
                    cn_ant_map = st.session_state.get('class_names_ant', {})
                    cn_ant = [cn_ant_map.get(i, f"Classe {i}") for i in range(n_cols_ant)]
                    df_matrix_ant = pd.DataFrame(matrix_ant,
                        columns=[f"Prev. {n}" for n in cn_ant],
                        index=[f"Real {n}" for n in cn_ant])
                    st.dataframe(df_matrix_ant, use_container_width=True)
                else:
                    st.warning("⚠️ Acurácia não calculada — poucas amostras por classe (< 6). Colete mais pontos para obter indicadores confiáveis.")

            with col2:
                st.write("**📅 Período Posterior (2025)**")
                accuracy_pos = st.session_state.get('accuracy_pos')
                kappa_pos = st.session_state.get('kappa_pos')
                if accuracy_pos is not None:
                    st.metric("Precisão Global", f"{accuracy_pos*100:.2f}%")
                    st.metric("Índice Kappa", f"{kappa_pos:.4f} — {_kappa_label(kappa_pos)}")
                    st.write("**Matriz de Confusão:**")
                    st.caption("Quanto mais números na diagonal (canto superior esquerdo → inferior direito), melhor a classificação.")
                    matrix_pos = st.session_state.get('matrix_pos', [[0]])
                    n_cols_pos = len(matrix_pos[0]) if matrix_pos else 0
                    cn_pos_map = st.session_state.get('class_names_pos', {})
                    cn_pos = [cn_pos_map.get(i, f"Classe {i}") for i in range(n_cols_pos)]
                    df_matrix_pos = pd.DataFrame(matrix_pos,
                        columns=[f"Prev. {n}" for n in cn_pos],
                        index=[f"Real {n}" for n in cn_pos])
                    st.dataframe(df_matrix_pos, use_container_width=True)
                else:
                    st.warning("⚠️ Acurácia não calculada — poucas amostras por classe (< 6). Colete mais pontos para obter indicadores confiáveis.")
            
            st.markdown("---")
            st.subheader("🗺️ Mapas Classificados")
            
            roi_resultados = obter_roi()
            palette_colors = [tipos_cobertura[tipo] for tipo in tipos_cobertura.keys()]

            vis_params_class = {
                'min': 0,
                'max': len(palette_colors) - 1,
                'palette': palette_colors
            }
            
            try:
                url_class_ant = st.session_state['classified_ant'].getThumbURL({
                    **vis_params_class,
                    'region': roi_resultados.bounds(),
                    'dimensions': 1200,
                    'format': 'png'
                })

                url_class_pos = st.session_state['classified_pos'].getThumbURL({
                    **vis_params_class,
                    'region': roi_resultados.bounds(),
                    'dimensions': 1200,
                    'format': 'png'
                })
                
                col1, col2 = st.columns(2)
                
                with col1:
                    st.write(f"**Classificação Anterior ({st.session_state.date_ant})**")
                    st.image(url_class_ant, use_container_width=True)
                
                with col2:
                    st.write(f"**Classificação Posterior ({st.session_state.date_pos})**")
                    st.image(url_class_pos, use_container_width=True)
                
                st.write("### 🎨 Legenda")
                legend_cols = st.columns(len(tipos_cobertura))
                for i, (tipo, cor) in enumerate(tipos_cobertura.items()):
                    with legend_cols[i]:
                        st.markdown(f"<div style='background-color:{cor}; padding:10px; border-radius:5px; text-align:center; color:white; font-weight:bold;'>{tipo}</div>", unsafe_allow_html=True)
            
            except Exception as e:
                st.error(f"Erro ao gerar visualizações: {e}")
            
            st.markdown("---")
            st.subheader("🔄 Análise de Mudanças")
            
            try:
                from_list = list(range(len(tipos_cobertura)))
                to_list = [1 if i == 0 else (3 if i == 2 else 2) for i in range(len(tipos_cobertura))]
                
                class_ant_remap = st.session_state['classified_ant'].remap(from_list, to_list, 0)
                class_pos_remap = st.session_state['classified_pos'].remap(from_list, to_list, 0)
                
                FF = 1
                AC = 2
                CH = 3
                DI = 4
                FR = 5
                
                analise = ee.Image(1) \
                    .where(class_ant_remap.eq(FF).And(class_pos_remap.eq(FF)), FF) \
                    .where(class_ant_remap.eq(FF).And(class_pos_remap.eq(AC)), DI) \
                    .where(class_ant_remap.eq(FF).And(class_pos_remap.eq(CH)), DI) \
                    .where(class_ant_remap.eq(AC).And(class_pos_remap.eq(AC)), AC) \
                    .where(class_ant_remap.eq(AC).And(class_pos_remap.eq(FF)), FR) \
                    .where(class_ant_remap.eq(AC).And(class_pos_remap.eq(CH)), CH) \
                    .where(class_ant_remap.eq(CH).And(class_pos_remap.eq(CH)), CH) \
                    .where(class_ant_remap.eq(CH).And(class_pos_remap.eq(FF)), FR) \
                    .where(class_ant_remap.eq(CH).And(class_pos_remap.eq(AC)), AC) \
                    .clip(st.session_state.roi)
                
                # Salvar imagem de análise para uso posterior
                st.session_state['change_image'] = analise
                
                palette_mudanca = ['#228B22', '#F5DEB3', '#4169E1', '#FF0000', '#90EE90']
                
                url_analise = analise.getThumbURL({
                    'min': 1,
                    'max': 5,
                    'palette': palette_mudanca,
                    'region': roi_resultados.bounds(),
                    'dimensions': 2048,
                    'format': 'png'
                })
                
                st.image(url_analise, caption="Mapa de Mudanças - VERMELHO = Desmatamento", use_container_width=True)
                
                st.write("### 🎨 Legenda de Mudanças")
                legend_mudanca = {
                    'Floresta Mantida': '#228B22',
                    'Área Consolidada': '#F5DEB3',
                    'Corpo Hídrico': '#4169E1',
                    'Desmatamento': '#FF0000',
                    'Regeneração': '#90EE90'
                }
                
                cols = st.columns(len(legend_mudanca))
                for i, (tipo, cor) in enumerate(legend_mudanca.items()):
                    with cols[i]:
                        st.markdown(f"<div style='background-color:{cor}; padding:10px; border-radius:5px; text-align:center; color:white; font-weight:bold;'>{tipo}</div>", unsafe_allow_html=True)
                
                st.write("### 📐 Cálculo de Áreas")
                
                with st.spinner("Calculando áreas..."):
                    areas = ee.Image.pixelArea().addBands(analise).reduceRegion(
                        reducer=ee.Reducer.sum().group(1),
                        geometry=st.session_state.roi,
                        scale=30,
                        maxPixels=1e13
                    ).getInfo()
                    
                    if 'groups' in areas:
                        areas_dict = {}
                        labels = ['Floresta Mantida', 'Área Consolidada', 'Corpo Hídrico', 'Desmatamento', 'Regeneração']
                        
                        for item in areas['groups']:
                            class_num = int(item['group'])  # Usar 'group' ao invés de 'class'
                            area_m2 = item['sum']
                            area_ha = area_m2 / 10000
                            if 1 <= class_num <= 5:
                                areas_dict[labels[class_num - 1]] = area_ha
                        
                        df_areas = pd.DataFrame([
                            {'Classe': k, 'Área (ha)': f"{v:,.2f}", 'Área (km²)': f"{v/100:,.2f}"}
                            for k, v in areas_dict.items()
                        ])
                        
                        st.dataframe(df_areas, use_container_width=True)
                        
                        st.write("### 📊 Métricas Principais")
                        
                        col1, col2, col3 = st.columns(3)
                        
                        with col1:
                            if 'Desmatamento' in areas_dict:
                                st.metric("🔴 Desmatamento", f"{areas_dict['Desmatamento']:,.2f} ha")
                        
                        with col2:
                            if 'Regeneração' in areas_dict:
                                st.metric("🟢 Regeneração", f"{areas_dict['Regeneração']:,.2f} ha")
                        
                        with col3:
                            if 'Floresta Mantida' in areas_dict:
                                st.metric("🌳 Floresta Mantida", f"{areas_dict['Floresta Mantida']:,.2f} ha")
                        
                        if 'Desmatamento' in areas_dict:
                            taxa_anual = areas_dict['Desmatamento'] / intervalo_anos
                            st.info(f"📈 Taxa anual de desmatamento: **{taxa_anual:.2f} ha/ano**")
                        
                        st.session_state['areas_dict'] = areas_dict
            
            except Exception as e:
                st.error(f"Erro na análise de mudanças: {e}")
                st.code(traceback.format_exc())

            # DOWNLOAD GEOTIFF
            st.markdown("---")
            st.subheader("⬇️ Download GeoTIFF")
            st.caption("Baixe os rasters classificados para uso em QGIS, ArcGIS ou outro sistema SIG (SIRGAS 2000 / EPSG:4674).")

            _col_dl1, _col_dl2, _col_dl3 = st.columns(3)
            _roi_dl = roi_resultados

            with _col_dl1:
                try:
                    _url_tiff_ant = st.session_state['classified_ant'].getDownloadURL({
                        'scale': 30,
                        'crs': 'EPSG:4674',
                        'region': _roi_dl,
                        'format': 'GeoTIFF'
                    })
                    st.link_button("⬇️ Baixar Classificação 2008 (GeoTIFF)", _url_tiff_ant, use_container_width=True)
                except Exception as _e:
                    st.warning(f"⚠️ Erro ao gerar link 2008: {_e}")

            with _col_dl2:
                try:
                    _url_tiff_pos = st.session_state['classified_pos'].getDownloadURL({
                        'scale': 30,
                        'crs': 'EPSG:4674',
                        'region': _roi_dl,
                        'format': 'GeoTIFF'
                    })
                    st.link_button("⬇️ Baixar Classificação 2025 (GeoTIFF)", _url_tiff_pos, use_container_width=True)
                except Exception as _e:
                    st.warning(f"⚠️ Erro ao gerar link 2025: {_e}")

            with _col_dl3:
                if 'change_image' in st.session_state:
                    try:
                        _url_tiff_change = st.session_state['change_image'].getDownloadURL({
                            'scale': 30,
                            'crs': 'EPSG:4674',
                            'region': _roi_dl,
                            'format': 'GeoTIFF'
                        })
                        st.link_button("⬇️ Baixar Análise de Mudanças (GeoTIFF)", _url_tiff_change, use_container_width=True)
                    except Exception as _e:
                        st.warning(f"⚠️ Erro ao gerar link mudanças: {_e}")

            # ANÁLISE POR LOTE - GERAR CSV
            if st.session_state.gdf_parcelas is not None:
                st.markdown("---")
                st.subheader("📊 Análise por Lote")
                
                with st.spinner("Calculando áreas por lote... Isso pode levar alguns minutos..."):
                    try:
                        
                        st.info(f"⚡ Processando {len(st.session_state.gdf_parcelas)} lotes em formato simplificado...")
                        
                        # Pegar imagens classificadas
                        classified_ant = st.session_state['classified_ant']
                        change_image = st.session_state['change_image']
                        
                        # ===== FORMATO COMPLETO conforme solicitado pela professora =====
                        # Header: Lote, Area_Total_ha, classes_2008, classes_mudanca
                        csv_data = []
                        header = ['Lote', 'Area_Total_ha']
                        
                        # Adicionar colunas das classes de 2008
                        for tipo in tipos_cobertura.keys():
                            header.append(f"{tipo}_2008_ha")
                        
                        # Adicionar colunas das classes de mudança (FF, AC, CH, DI, FR)
                        header.extend(['FF_ha', 'AC_ha', 'CH_ha', 'DI_ha', 'FR_ha'])
                        
                        csv_data.append(header)
                        
                        st.write("⚡ Processando classificação e análise de mudança por lote...")
                        
                        # Converter parcelas para FeatureCollection do GEE
                        st.write("📦 Preparando lotes...")
                        
                        # CRÍTICO: Garantir que GEE está inicializado
                        inicializar_gee()
                        
                        parcelas_features = []
                        lotes_info = {}  # Guardar nome e área
                        lotes_com_erro = []

                        # Reprojetar parcelas para UTM para cálculo de área preciso
                        _c = st.session_state.gdf_parcelas.geometry.unary_union.centroid
                        _zone = int((_c.x + 180) / 6) + 1
                        _epsg_utm = 32600 + _zone if _c.y >= 0 else 32700 + _zone
                        _gdf_parcelas_utm = st.session_state.gdf_parcelas.to_crs(epsg=_epsg_utm)

                        for idx, row in st.session_state.gdf_parcelas.iterrows():
                            try:
                                geom = row.geometry
                                
                                # Validar geometria
                                if geom is None or geom.is_empty:
                                    lotes_com_erro.append(idx)
                                    continue
                                
                                # Garantir geometria válida
                                if not geom.is_valid:
                                    geom = geom.buffer(0)
                                
                                _COLS_LOTE = ['NOM_LOT', 'nom_lot', 'NUM_LOTE', 'num_lote',
                                              'Lote', 'lote', 'LOTE', 'PARCELA', 'parcela',
                                              'Name', 'name', 'ID_LOTE', 'id_lote']
                                nome = None
                                for _col in _COLS_LOTE:
                                    if _col in st.session_state.gdf_parcelas.columns:
                                        _val = str(row[_col]).strip()
                                        if _val and _val not in ('nan', 'None', ''):
                                            nome = _val
                                            break
                                if not nome:
                                    nome = f'Lote_{idx + 1}'
                                geom_utm = _gdf_parcelas_utm.loc[idx].geometry
                                if not geom_utm.is_valid:
                                    geom_utm = geom_utm.buffer(0)
                                area_lote_ha = geom_utm.area / 10000
                                
                                lotes_info[idx] = {
                                    'nome': nome,
                                    'area_ha': round(area_lote_ha, 2)
                                }
                                
                                ee_geom = _geom_para_gee(geom)
                                feature = ee.Feature(ee_geom, {'lote_id': idx})
                                parcelas_features.append(feature)
                                
                            except Exception as e:
                                lotes_com_erro.append(idx)
                                st.warning(f"⚠️ Erro no lote {idx}: {e}")
                        
                        if lotes_com_erro:
                            st.warning(f"⚠️ {len(lotes_com_erro)} lotes com geometrias inválidas foram ignorados.")
                        
                        if len(parcelas_features) == 0:
                            st.error("❌ Nenhum lote válido para processar!")
                            st.stop()
                        
                        parcelas_fc = ee.FeatureCollection(parcelas_features)
                        st.success(f"✅ {len(parcelas_features)} lotes preparados para análise.")

                        
                        # Dicionário para armazenar áreas das classes de 2008
                        classes_2008 = {tipo: {} for tipo in tipos_cobertura.keys()}
                        
                        # Processar cada classe de 2008 em BATCH
                        st.write("⏳ Calculando áreas - Classificação 2008...")
                        for idx_tipo, tipo in enumerate(tipos_cobertura.keys()):
                            try:
                                classe_num = idx_tipo
                                st.write(f"   → {tipo}...")
                                
                                classe_mask = classified_ant.eq(classe_num).multiply(ee.Image.pixelArea())
                                
                                results = classe_mask.reduceRegions(
                                    collection=parcelas_fc,
                                    reducer=ee.Reducer.sum(),
                                    scale=30
                                ).getInfo()
                                
                                for feature in results['features']:
                                    lote_id = feature['properties']['lote_id']
                                    area_m2 = feature['properties'].get('sum') or 0
                                    area_ha = area_m2 / 10000
                                    classes_2008[tipo][lote_id] = round(area_ha, 2) if area_ha > 0.05 else 0.0
                            
                            except Exception as e:
                                st.warning(f"⚠️ Erro ao processar classe '{tipo}': {e}")
                                # Inicializar com zeros para não quebrar CSV
                                for idx in lotes_info.keys():
                                    classes_2008[tipo][idx] = 0.0
                        
                        # Dicionário para armazenar áreas das 5 classes de mudança
                        classes_mudanca = {
                            1: {},  # FF - Floresta mantida
                            2: {},  # AC - Área consolidada
                            3: {},  # CH - Corpos d'água
                            4: {},  # DI - Desmatamento
                            5: {}   # FR - Floresta regenerada
                        }
                        
                        # Processar cada classe de mudança em BATCH
                        st.write("⏳ Calculando áreas - Análise de Mudança...")
                        nomes_classes = {
                            1: 'FF (Floresta Mantida)',
                            2: 'AC (Área Consolidada)',
                            3: 'CH (Corpos d\'Água)',
                            4: 'DI (Desmatamento)',
                            5: 'FR (Regeneração)'
                        }
                        
                        for classe_num in [1, 2, 3, 4, 5]:
                            try:
                                st.write(f"   → {nomes_classes[classe_num]}...")
                                
                                classe_mask = change_image.eq(classe_num).multiply(ee.Image.pixelArea())
                                
                                results = classe_mask.reduceRegions(
                                    collection=parcelas_fc,
                                    reducer=ee.Reducer.sum(),
                                    scale=30
                                ).getInfo()
                                
                                for feature in results['features']:
                                    lote_id = feature['properties']['lote_id']
                                    area_m2 = feature['properties'].get('sum') or 0
                                    area_ha = area_m2 / 10000
                                    classes_mudanca[classe_num][lote_id] = round(area_ha, 2) if area_ha > 0.05 else 0.0
                            
                            except Exception as e:
                                st.warning(f"⚠️ Erro ao processar {nomes_classes[classe_num]}: {e}")
                                # Inicializar com zeros
                                for idx in lotes_info.keys():
                                    classes_mudanca[classe_num][idx] = 0.0
                        
                        # Montar CSV com TODAS as colunas
                        st.write("📊 Montando planilha...")
                        for idx in sorted(lotes_info.keys()):
                            # Linha: Nome, Área, Classes_2008, Classes_Mudança
                            lote_row = [
                                lotes_info[idx]['nome'],
                                lotes_info[idx]['area_ha']
                            ]
                            
                            # Adicionar áreas das classes de 2008
                            for tipo in tipos_cobertura.keys():
                                area = classes_2008[tipo].get(idx, 0.0)
                                lote_row.append(area if area > 0 else '')
                            
                            # Adicionar áreas das classes de mudança (FF, AC, CH, DI, FR)
                            for classe_num in [1, 2, 3, 4, 5]:
                                area = classes_mudanca[classe_num].get(idx, 0.0)
                                lote_row.append(area if area > 0 else '')
                            
                            csv_data.append(lote_row)
                        
                        # Converter para CSV no formato correto
                        csv_buffer = io.StringIO()
                        writer = csv.writer(csv_buffer, quoting=csv.QUOTE_ALL)
                        for row_data in csv_data:
                            row_str = ['' if (val == '' or val is None) else str(val) for val in row_data]
                            writer.writerow(row_str)
                        
                        csv_string = csv_buffer.getvalue()
                        st.session_state['csv_lotes'] = csv_string
                        
                        st.success(f"✅ Análise concluída! {len(csv_data)-1} lotes processados com classificação 2008 e análise de mudança.")
                        
                        # Mostrar legenda atualizada
                        st.info("""
                        **Colunas do CSV:**
                        - **Lote:** Nome do lote
                        - **Area_Total_ha:** Área total do lote (hectares)
                        - **[Classes]_2008_ha:** Áreas de cada classe na classificação de 2008 (Floresta, Pastagem, Água, Outra Vegetação, Solo Exposto, Queimada, Agricultura)
                        - **FF_ha:** Floresta Mantida (verde escuro)
                        - **AC_ha:** Área Consolidada - não floresta (bege)
                        - **CH_ha:** Corpos d'Água (azul)
                        - **DI_ha:** Desmatamento/Incremento (vermelho)
                        - **FR_ha:** Floresta Regenerada (verde claro)
                        """)
                        
                        # Botão download
                        st.download_button(
                            label="📥 Baixar Planilha de Lotes (CSV)",
                            data=csv_string,
                            file_name=f"analise_lotes_{st.session_state.date_ant}_{st.session_state.date_pos}.csv",
                            mime="text/csv"
                        )
                        
                        # Mostrar preview
                        with st.expander("👁️ Visualizar dados"):
                            st.dataframe(pd.read_csv(io.StringIO(csv_string)))
                    
                    except Exception as e:
                        st.error(f"❌ Erro ao processar lotes: {e}")
                        st.code(traceback.format_exc())
            
            st.markdown("---")
            st.subheader("📄 Gerar Relatório")
            
            if st.button("📥 Gerar Relatório PDF", type="primary"):
                try:
                    pdf = FPDF()
                    pdf.add_page()
                    pdf.set_font("Arial", "B", 16)
                    pdf.cell(0, 10, "DARC - Relatorio de Analise de Desmatamento", ln=True, align="C")
                    pdf.ln(5)
                    
                    pdf.set_font("Arial", "", 12)
                    pdf.cell(0, 10, "Instituto Federal de Rondonia", ln=True, align="C")
                    pdf.ln(10)
                    
                    pdf.set_font("Arial", "B", 14)
                    pdf.cell(0, 10, "1. Informacoes do Projeto", ln=True)
                    pdf.set_font("Arial", "", 11)
                    area_ha = calcular_area_ha(st.session_state.gdf)
                    pdf.cell(0, 7, f"Area Total: {area_ha:,.2f} ha", ln=True)
                    pdf.cell(0, 7, f"Periodo Anterior: {st.session_state.date_ant}", ln=True)
                    pdf.cell(0, 7, f"Periodo Posterior: {st.session_state.date_pos}", ln=True)
                    pdf.cell(0, 7, f"Intervalo: {intervalo_anos:.1f} anos", ln=True)
                    pdf.ln(5)
                    
                    pdf.set_font("Arial", "B", 14)
                    pdf.cell(0, 10, "2. Acuracia da Classificacao", ln=True)
                    pdf.set_font("Arial", "", 11)
                    
                    pdf.cell(0, 7, f"Periodo Anterior (2008):", ln=True)
                    _acc_ant = st.session_state.get('accuracy_ant')
                    _kap_ant = st.session_state.get('kappa_ant')
                    if _acc_ant is not None:
                        pdf.cell(0, 7, f"  - Precisao Global: {_acc_ant*100:.2f}%", ln=True)
                        pdf.cell(0, 7, f"  - Indice Kappa: {_kap_ant:.4f}", ln=True)
                    else:
                        pdf.cell(0, 7, "  - Precisao Global: N/A (poucas amostras)", ln=True)
                        pdf.cell(0, 7, "  - Indice Kappa: N/A (poucas amostras)", ln=True)
                    pdf.ln(3)

                    pdf.cell(0, 7, f"Periodo Posterior (2025):", ln=True)
                    _acc_pos = st.session_state.get('accuracy_pos')
                    _kap_pos = st.session_state.get('kappa_pos')
                    if _acc_pos is not None:
                        pdf.cell(0, 7, f"  - Precisao Global: {_acc_pos*100:.2f}%", ln=True)
                        pdf.cell(0, 7, f"  - Indice Kappa: {_kap_pos:.4f}", ln=True)
                    else:
                        pdf.cell(0, 7, "  - Precisao Global: N/A (poucas amostras)", ln=True)
                        pdf.cell(0, 7, "  - Indice Kappa: N/A (poucas amostras)", ln=True)
                    pdf.ln(5)
                    
                    pdf.set_font("Arial", "B", 14)
                    pdf.cell(0, 10, "3. Areas de Mudanca", ln=True)
                    pdf.set_font("Arial", "", 11)
                    
                    if 'areas_dict' in st.session_state:
                        for classe, area in st.session_state['areas_dict'].items():
                            pdf.cell(0, 7, f"{classe}: {area:,.2f} ha", ln=True)
                        
                        if 'Desmatamento' in st.session_state['areas_dict']:
                            taxa = st.session_state['areas_dict']['Desmatamento'] / intervalo_anos
                            pdf.ln(5)
                            pdf.set_font("Arial", "B", 11)
                            pdf.cell(0, 7, f"Taxa Anual: {taxa:.2f} ha/ano", ln=True)
                    
                    # Gerar PDF em memória
                    pdf_output = BytesIO()
                    pdf_content = pdf.output()
                    
                    # FPDF retorna bytearray, converter para bytes
                    if isinstance(pdf_content, bytearray):
                        pdf_output.write(bytes(pdf_content))
                    elif isinstance(pdf_content, bytes):
                        pdf_output.write(pdf_content)
                    else:
                        pdf_output.write(pdf_content.encode('latin1'))
                    
                    pdf_output.seek(0)
                    
                    st.download_button(
                        label="📥 Baixar Relatório PDF",
                        data=pdf_output,
                        file_name=f"relatorio_darc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
                        mime="application/pdf"
                    )
                    
                    st.success("✅ Relatório gerado!")
                
                except Exception as e:
                    st.error(f"Erro ao gerar relatório: {e}")

else:
    st.info("👆 **Comece fazendo upload do shapefile do PA** (perímetro OU lotes - se enviar só lotes, o perímetro será calculado automaticamente)")

st.markdown("---")
st.caption("DARC - Deforestation Analysis for Rural Settlements | IFRO 2025")
