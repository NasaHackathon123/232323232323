import streamlit as st
import streamlit.components.v1 as components

# Configure Streamlit page layout for a scientific geospatial dashboard
st.set_page_config(
    page_title="EmberWatch — NASA MODIS & VIIRS Active Fire Observatory",
    page_icon="🔥",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Hide default Streamlit header/footer for an authentic web application look
hide_streamlit_style = """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    .block-container {padding-top: 0rem; padding-bottom: 0rem; padding-left: 0rem; padding-right: 0rem;}
    </style>
"""
st.markdown(hide_streamlit_style, unsafe_allow_html=True)

# Complete EmberWatch Editorial & Geospatial Frontend HTML/JS Payload
EMBERWATCH_HTML = """
<!DOCTYPE html>
<html lang="en" class="h-full">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>EmberWatch — NASA MODIS & VIIRS Active Fire Observatory</title>
    <!-- Tailwind CSS -->
    <script src="https://cdn.tailwindcss.com"></script>
    <!-- Leaflet CSS & JS -->
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <!-- Chart.js -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <!-- Google Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&display=swap" rel="stylesheet">
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        navy: { 900: '#13262E', 800: '#1A333E', 700: '#234453' },
                        paper: { 50: '#F6F7F5', 100: '#ECEEE9', 200: '#D2D7D3' },
                        fire: { orange: '#C85624', amber: '#C98E2B', crimson: '#9E2A22' },
                        forest: { sage: '#5A7265', dark: '#1F3129' }
                    },
                    fontFamily: {
                        serif: ['Newsreader', 'serif'],
                        sans: ['IBM Plex Sans', 'sans-serif'],
                        mono: ['IBM Plex Mono', 'monospace']
                    }
                }
            }
        }
    </script>
</head>
<body class="bg-paper-50 text-navy-900 font-sans antialiased h-full flex flex-col selection:bg-fire-orange selection:text-white">

    <!-- Top Archival Navigation Bar -->
    <header class="bg-navy-900 text-paper-50 border-b border-navy-700 sticky top-0 z-50">
        <div class="max-w-7xl mx-auto px-6 h-16 flex items-center justify-between">
            <div class="flex items-center space-x-4">
                <div class="w-8 h-8 bg-fire-orange flex items-center justify-center font-serif font-bold text-white text-lg tracking-tighter">E</div>
                <div>
                    <span class="font-serif text-xl tracking-tight font-medium text-white">EmberWatch</span>
                    <span class="text-xs font-mono uppercase tracking-widest text-paper-200 ml-3 pl-3 border-l border-navy-700 hidden sm:inline">NASA MODIS & VIIRS Observatory</span>
                </div>
            </div>
            <nav class="hidden md:flex items-center space-x-1 font-mono text-xs">
                <button onclick="switchTab('overview')" id="nav-overview" class="px-3 py-1.5 transition-colors bg-navy-800 text-white border-b-2 border-fire-orange">01. Overview</button>
                <button onclick="switchTab('map')" id="nav-map" class="px-3 py-1.5 transition-colors text-paper-200 hover:text-white">02. GIS Map</button>
                <button onclick="switchTab('calendar')" id="nav-calendar" class="px-3 py-1.5 transition-colors text-paper-200 hover:text-white">03. Activity Calendar</button>
                <button onclick="switchTab('analytics')" id="nav-analytics" class="px-3 py-1.5 transition-colors text-paper-200 hover:text-white">04. Analytics</button>
                <button onclick="switchTab('harmonisation')" id="nav-harmonisation" class="px-3 py-1.5 transition-colors text-paper-200 hover:text-white">05. Harmonisation</button>
                <button onclick="switchTab('methodology')" id="nav-methodology" class="px-3 py-1.5 transition-colors text-paper-200 hover:text-white">06. Methodology</button>
            </nav>
            <div class="flex items-center space-x-3 text-xs font-mono">
                <span class="hidden lg:inline text-paper-200">Region:</span>
                <select id="region-selector" onchange="updateRegion()" class="bg-navy-800 text-white border border-navy-700 px-2.5 py-1 text-xs focus:outline-none focus:border-fire-orange font-mono">
                    <option value="sea">Maritime Southeast Asia (Default)</option>
                    <option value="johor">Peninsular Malaysia & Johor</option>
                    <option value="sumatra">Sumatra & Riau Peatlands</option>
                    <option value="borneo">Kalimantan & Borneo</option>
                </select>
            </div>
        </div>
    </header>

    <!-- Main Content Viewport -->
    <main class="flex-1 overflow-y-auto">

        <!-- TAB 01: OVERVIEW -->
        <section id="view-overview" class="p-6 md:p-10 max-w-7xl mx-auto space-y-8">
            <div class="grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
                <div class="lg:col-span-7 space-y-6">
                    <div class="inline-flex items-center space-x-2 text-xs font-mono bg-paper-100 text-navy-800 px-3 py-1 border border-paper-200">
                        <span class="w-2 h-2 rounded-full bg-fire-orange animate-pulse"></span>
                        <span>NASA Space Apps Challenge 2026 • Harmonised Hotspot Engine</span>
                    </div>
                    <h1 class="font-serif text-4xl md:text-5xl lg:text-6xl font-normal leading-tight text-navy-900">
                        Understanding Earth's Burning Activity.
                    </h1>
                    <p class="text-lg text-forest-sage font-light leading-relaxed max-w-2xl">
                        Synthesising spaceborne thermal anomalies from NASA Terra/Aqua MODIS and Suomi-NPP/NOAA-20 VIIRS instruments into a unified, high-fidelity Burning Activity Calendar for Southeast Asia and global ecosystems.
                    </p>
                    <div class="flex flex-wrap gap-4 pt-2">
                        <button onclick="switchTab('map')" class="bg-navy-900 text-white px-6 py-3 font-mono text-xs hover:bg-navy-800 transition-colors flex items-center space-x-2">
                            <span>Launch GIS Map Explorer</span>
                            <span class="text-fire-orange">→</span>
                        </button>
                        <button onclick="switchTab('calendar')" class="bg-white border border-paper-200 text-navy-900 px-6 py-3 font-mono text-xs hover:bg-paper-100 transition-colors">
                            View Burning Activity Calendar
                        </button>
                    </div>
                </div>

                <!-- Live Regional Summary Card -->
                <div class="lg:col-span-5 bg-white border border-paper-200 p-6 space-y-6 shadow-sm">
                    <div class="flex justify-between items-center border-b border-paper-200 pb-4">
                        <h3 class="font-mono text-xs uppercase tracking-wider text-forest-sage">Observation Parameters</h3>
                        <span id="overview-region-badge" class="font-mono text-xs bg-paper-100 px-2 py-0.5 text-navy-900">SE Asia (1.5°N, 108.2°E)</span>
                    </div>
                    <div class="grid grid-cols-2 gap-4">
                        <div class="p-4 bg-paper-50 border border-paper-200">
                            <span class="text-xs font-mono text-forest-sage block">MODIS (1 km)</span>
                            <span id="stat-modis" class="text-2xl font-serif text-navy-900 font-medium">1,428</span>
                            <span class="text-[10px] font-mono text-forest-sage block mt-1">Terra (MOD14
