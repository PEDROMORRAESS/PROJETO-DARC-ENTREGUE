# 🌳 DARC - Detecção Automática de Remoção de Cobertura Vegetal

**Sistema científico para detecção de desmatamento ilegal em Projetos de Assentamento usando classificação supervisionada e sensoriamento remoto.**

---

## 📋 Visão Geral

DARC detecta **desmatamento ilegal** em Reservas Legais de Projetos de Assentamento (PAs) do INCRA através de análise multitemporal de imagens Landsat.

### Objetivo
Identificar mudanças na cobertura vegetal entre dois períodos (ex: 2008 vs 2025) para detectar:
- ✅ **Desmatamento ilegal** (floresta → área consolidada)
- ✅ **Regeneração florestal** (área consolidada → floresta)
- ✅ **Áreas preservadas** (floresta → floresta)

### Dados
**100% REAIS** - Imagens NASA/USGS Landsat (sem dados sintéticos)

---

## 🔬 Metodologia Científica

### 1. Aquisição de Imagens

**Satélites:**
- **Período Anterior (2008):** Landsat 5 TM (Thematic Mapper)
- **Período Posterior (2025):** Landsat 9 OLI-2 (Operational Land Imager)

**Parâmetros:**
- Resolução espacial: 30m
- Cobertura de nuvens: < 80% (configurável)
- Sistema de coordenadas: EPSG:4326 (WGS84)

**Bandas utilizadas:**

| Landsat 5 TM | Landsat 9 OLI-2 | Descrição |
|--------------|-----------------|-----------|
| SR_B1 (Blue) | SR_B2 (Blue) | Azul |
| SR_B2 (Green) | SR_B3 (Green) | Verde |
| SR_B3 (Red) | SR_B4 (Red) | Vermelho |
| SR_B4 (NIR) | SR_B5 (NIR) | Infravermelho Próximo |
| SR_B5 (SWIR1) | SR_B6 (SWIR1) | Infravermelho de Ondas Curtas 1 |
| SR_B7 (SWIR2) | SR_B7 (SWIR2) | Infravermelho de Ondas Curtas 2 |

---

### 2. Índices Espectrais

Quatro índices são calculados para cada período:

#### **NDVI - Índice de Vegetação por Diferença Normalizada**
```
NDVI = (NIR - Red) / (NIR + Red)
```
- **Uso:** Identificar vegetação verde e vigor
- **Valores:** -1 a +1 (>0.3 = vegetação densa)

#### **SAVI - Índice de Vegetação Ajustado ao Solo**
```
SAVI = ((NIR - Red) / (NIR + Red + 0.5)) × 1.5
```
- **Uso:** Vegetação em áreas com solo exposto
- **Diferencial:** Minimiza influência do solo

#### **NBR - Razão Normalizada de Queimada**
```
NBR = (NIR - SWIR2) / (NIR + SWIR2)
```
- **Uso:** Detectar áreas queimadas
- **Valores:** Altos = vegetação saudável, Baixos = queimada

#### **MNDWI - Índice de Água Modificado**
```
MNDWI = (Green - SWIR1) / (Green + SWIR1)
```
- **Uso:** Identificar corpos d'água
- **Diferencial:** Melhor que NDWI para vegetação úmida

---

### 3. Classificação Supervisionada (Random Forest)

#### **Classes de Cobertura**

| ID | Nome | Cor | Descrição |
|----|------|-----|-----------|
| 0 | Floresta | Verde escuro | Formação florestal nativa |
| 1 | Pastagem | Bege | Pasto, vegetação rasteira |
| 2 | Água | Azul | Corpos d'água |
| 3 | Outra Vegetação | Verde claro | Vegetação secundária |
| 4 | Solo Exposto | Marrom | Solo nu, estradas |
| 5 | Queimada | Vermelho | Áreas queimadas |
| 6 | Agricultura | Amarelo | Plantações |

#### **Algoritmo: Random Forest**

**Parâmetros:**
- **Árvores:** 50
- **Divisão treino/validação:** 70% / 30%
- **Features:** 10 bandas (6 espectrais + 4 índices)

**Processo:**
1. Coleta de amostras (pontos de treinamento)
2. Divisão aleatória (70% treino, 30% validação)
3. Treinamento do classificador
4. Classificação da imagem completa
5. Validação com matriz de confusão

---

### 4. Reclassificação

**Objetivo:** Simplificar classes para análise de reserva legal

| Classes Originais | Classe Final | Código |
|-------------------|--------------|--------|
| Floresta | Formação Florestal | 1 |
| Pastagem, Outra Veg, Solo, Queimada, Agricultura | Área Consolidada | 2 |
| Água | Corpo Hídrico | 3 |

---

### 5. Filtro Majority (Passa-Baixa)

**Função:** Remover pixels isolados (ruído)

```python
Kernel: Manhattan(1)  # 3x3 pixels
Método: Mode (moda dos vizinhos)
```

**Efeito:** Suaviza classificação e melhora acurácia visual

---

### 6. Análise Booleana da Reserva Legal

**Matriz de Mudança:**

| 2008 → 2025 | Resultado | Código | Cor |
|-------------|-----------|--------|-----|
| Floresta → Floresta | Área Preservada | 1 | Verde escuro |
| Floresta → Área Consolidada | **DESMATAMENTO ILEGAL** | 4 | Vermelho |
| Floresta → Água | Desmatamento Ilegal | 4 | Vermelho |
| Área Consolidada → Floresta | Regeneração | 5 | Verde claro |
| Área Consolidada → Área Consolidada | Área Consolidada | 2 | Bege |
| Água → Água | Corpo Hídrico | 3 | Azul |

**Regra:** Floresta em 2008 que virou outra classe = **DESMATAMENTO ILEGAL**

---

### 7. Cálculo de Áreas

**Áreas calculadas:**
- Área total do PA (perímetro)
- Área por classe (2008 e 2025)
- Área de desmatamento ilegal (total e por lote)
- Área de regeneração
- Área por lote individual

**Unidade:** Hectares (ha)

**Fórmula:**
```
Área (ha) = (Quantidade de pixels × 30m × 30m) / 10.000
```

---

## 📊 Métricas de Validação

### Matriz de Confusão

Compara classificação vs amostras de validação:

```
                Previsto
              0   1   2   3
        0  [94   3   1   2]  ← Real
Real    1  [ 2  87   5   6]
        2  [ 1   4  92   3]
        3  [ 3   6   2  89]
```

### Índice Kappa (κ)

**Fórmula:**
```
κ = (Po - Pe) / (1 - Pe)

Onde:
Po = Proporção observada de concordância
Pe = Proporção esperada por acaso
```

**Interpretação:**
- κ < 0.40: Ruim
- 0.40 ≤ κ < 0.60: Moderado
- 0.60 ≤ κ < 0.80: Bom
- κ ≥ 0.80: Excelente

### Acurácia

**Acurácia Global (Overall Accuracy):**
```
OA = Soma da diagonal / Total de amostras
```

**Acurácia do Produtor (Producer's Accuracy):**
```
PA = Corretos da classe / Total real da classe
```
Indica: "% de pixels reais da classe X que foram corretamente identificados"

**Acurácia do Usuário (Consumer's Accuracy):**
```
UA = Corretos da classe / Total previsto da classe
```
Indica: "% de pixels classificados como X que realmente são X"

---

## 🚀 Quick Start

### Pré-requisitos
- Python 3.10+
- Conta Google Earth Engine
- 4GB RAM mínimo

### Instalação

```bash
# 1. Clonar/baixar arquivos
cd C:\projetos\darc

# 2. Criar ambiente virtual
python -m venv venv
venv\Scripts\activate

# 3. Instalar dependências
pip install -r requirements.txt

# 4. Autenticar Earth Engine
earthengine authenticate

# 5. Rodar aplicação
streamlit run app.py
```

**Acesse:** http://localhost:8501

---

## 📁 Estrutura do Projeto

```
darc/
├── app.py                  # Aplicação Streamlit principal
├── requirements.txt        # Dependências Python
├── README.md              # Esta documentação
├── .env.example           # Exemplo de variáveis de ambiente
├── .gitignore             # Arquivos a ignorar
└── data/                  # Shapefiles de teste
    ├── PA_perimetro.zip
    └── PA_parcelas.zip
```

---

## 🎯 Uso do Sistema

### Passo 1: Upload de Shapefiles
- **Perímetro:** Limite externo do PA (obrigatório)
- **Lotes:** Parcelas individuais (opcional, para análise por lote)

### Passo 2: Configurar Datas
- **Período Anterior:** Data de referência (ex: 2008-07-18)
- **Período Posterior:** Data atual (ex: 2025-08-15)

### Passo 3: Buscar Imagens
- Sistema busca automaticamente no Google Earth Engine
- Filtra por cobertura de nuvens (máx 80%)
- Exibe imagens RGB dos dois períodos

### Passo 4: Coletar Amostras
- Clique no mapa para marcar pontos
- **Mínimo:** 2 classes (ex: Floresta + Pastagem)
- **Recomendado:** 50-100 pontos por classe
- Alterne entre "Período Anterior" e "Posterior"

### Passo 5: Processar Análise
- Sistema treina classificador Random Forest
- Classifica imagens
- Calcula mudanças
- Gera relatório PDF

### Passo 6: Resultados
- **Mapas:** Classificação 2008, 2025 e análise de mudança
- **Relatório PDF:** Completo com métricas científicas
- **CSV:** Áreas por lote (se aplicável)

---

## 📈 Saídas do Sistema

### 1. Mapas Interativos
- Classificação Período Anterior
- Classificação Período Posterior
- Análise da Reserva Legal (mudanças)

### 2. Relatório Científico (PDF)
**Seções:**
1. Informações do Projeto
2. Metodologia
3. Classificação (2 períodos)
4. Matriz de Confusão
5. Métricas de Acurácia (Kappa, OA, PA, UA)
6. Análise de Mudanças
7. Tabelas de Áreas
8. Conclusão

### 3. Dados Tabulares (CSV)
- Área total por classe
- Área de desmatamento por lote
- Coordenadas das amostras

---

## 🔧 Configurações Avançadas

### Porcentagem de Nuvens
```python
cloud_cover = 80  # Padrão: 80%
# Reduzir para áreas críticas: 50%
# Aumentar se nenhuma imagem disponível: 90%
```

### Número de Árvores (Random Forest)
```python
n_trees = 50  # Padrão
# Aumentar para maior acurácia: 100
# Reduzir para velocidade: 30
```

### Janela Temporal
```python
window = 6  # ±6 meses da data alvo
# Ampliar se imagens insuficientes: 12 meses
```

---

## ⚠️ Limitações

### Dados
- ✅ Landsat 5: Disponível desde 1984
- ❌ Landsat 5: Encerrado em 2013
- ✅ Landsat 9: Disponível desde 2021
- ⚠️ Lacunas: 2013-2021 usar Landsat 7/8

### Cobertura de Nuvens
- Áreas com muitas nuvens podem ter dados parciais
- Solução: Testar outras datas ou criar mosaico

### Resolução
- 30m = 900m² por pixel
- Objetos menores que 30m podem não ser detectados

---

## 📚 Referências Científicas

### Algoritmos
- **Random Forest:** Breiman, L. (2001). Random Forests. Machine Learning 45, 5–32.
- **Kappa Statistic:** Cohen, J. (1960). A coefficient of agreement for nominal scales.

### Índices Espectrais
- **NDVI:** Rouse et al. (1974). Monitoring vegetation systems in the Great Plains with ERTS.
- **SAVI:** Huete, A. (1988). A soil-adjusted vegetation index (SAVI).
- **NBR:** Key, C. H., & Benson, N. C. (2006). Landscape Assessment.
- **MNDWI:** Xu, H. (2006). Modification of normalized difference water index (NDWI).

### Landsat
- **NASA/USGS:** Landsat Collection 2 Level-2 Surface Reflectance
- **Google Earth Engine:** Gorelick et al. (2017). Google Earth Engine: Planetary-scale geospatial analysis.

---

## 🆘 Troubleshooting

### "Nenhuma imagem encontrada"
- ✅ Amplie janela temporal
- ✅ Aumente % de nuvens permitida
- ✅ Teste outras datas

### "Erro: Apenas 1 classe"
- ✅ Colete amostras de pelo menos 2 classes
- ✅ Verifique se alternou entre períodos

### "Mapa mostra apenas parte da imagem"
- ✅ Imagem Landsat não cobre área completa
- ✅ Teste outras datas
- ✅ Área pode estar na borda da cena

### Performance lenta
- ✅ Reduza quantidade de marcadores (< 30)
- ✅ Use período específico, não mosaico
- ✅ Desmarque "Mostrar lotes"

---

## 🤝 Contribuindo

Sistema desenvolvido para análise científica acadêmica. Para melhorias:

1. Documente o problema/sugestão
2. Teste localmente
3. Valide resultados cientificamente

---

## 📄 Licença

Uso acadêmico e científico.

---

## 👨‍💻 Autor

Desenvolvido para análise de desmatamento em Projetos de Assentamento (INCRA/MT).

---

**🌳 DARC - Protegendo nossas florestas através da ciência! 🌳**
