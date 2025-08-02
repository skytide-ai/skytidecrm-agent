
import os

file_path = 'appointment_agent.py'
lines = []
corrected = False

# Leer el archivo
with open(file_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Corregir la línea problemática
for i, line in enumerate(lines):
    if 'print(f"Cita creada con éxito. ID: {appointment_id}")' in line:
        # La indentación correcta debe ser de 8 espacios en este punto del código
        lines[i] = ' ' * 8 + 'print(f"Cita creada con éxito. ID: {appointment_id}")\n'
        corrected = True
        print(f"Línea {i+1} corregida.")
        break

# Guardar el archivo corregido
if corrected:
    with open(file_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    print(f"Archivo '{file_path}' guardado con la corrección.")
else:
    print("No se encontró la línea a corregir.")
