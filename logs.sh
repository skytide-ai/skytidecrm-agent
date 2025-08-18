#!/bin/bash

# Script para ver logs de manera organizada
# Uso: ./logs.sh [servicio] [filtro]

SERVICE=${1:-"all"}
FILTER=${2:-""}

echo "================================================"
echo "     SKYTIDECRM LOGS VIEWER"
echo "================================================"
echo ""

case $SERVICE in
  gateway|express)
    echo "📡 GATEWAY LOGS:"
    echo "----------------"
    if [ -z "$FILTER" ]; then
      docker-compose logs --tail=50 -f express-gateway
    else
      docker-compose logs --tail=100 express-gateway | grep -i "$FILTER"
    fi
    ;;
    
  python|agent)
    echo "🤖 PYTHON AGENT LOGS:"
    echo "--------------------"
    if [ -z "$FILTER" ]; then
      docker-compose logs --tail=50 -f python-service
    else
      docker-compose logs --tail=100 python-service | grep -i "$FILTER"
    fi
    ;;
    
  errors|error)
    echo "❌ ERRORS (Últimas 2 horas):"
    echo "----------------------------"
    docker-compose logs --since 2h | grep -E "ERROR|error|Error|FAIL|fail|Fail"
    ;;
    
  webhooks|webhook)
    echo "🔍 WEBHOOKS (Últimos 50):"
    echo "------------------------"
    docker-compose logs express-gateway | grep -E "WEBHOOK|webhook|Gupshup" | tail -50
    ;;
    
  media)
    echo "📎 MEDIA PROCESSING:"
    echo "-------------------"
    docker-compose logs express-gateway | grep -E "Media|media|audio|image|📎" | tail -50
    ;;
    
  all|*)
    echo "📊 TODOS LOS SERVICIOS:"
    echo "----------------------"
    docker-compose logs --tail=30 -f
    ;;
esac

echo ""
echo "================================================"
echo "Comandos útiles:"
echo "  ./logs.sh gateway         - Solo logs del gateway"
echo "  ./logs.sh python          - Solo logs del agente"
echo "  ./logs.sh errors          - Solo errores"
echo "  ./logs.sh webhooks        - Solo webhooks"
echo "  ./logs.sh media           - Solo procesamiento de media"
echo "  ./logs.sh all             - Todos los servicios"
echo "  ./logs.sh gateway 'org-123' - Filtrar por organización"
echo "================================================"