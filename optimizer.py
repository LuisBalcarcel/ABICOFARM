from ortools.sat.python import cp_model

def generar_horario_semana(empleados, farmacias, dias=7, ausencias=None, forzadas=None):
    if ausencias is None:
        ausencias = []
    if forzadas is None:
        forzadas = []

    model = cp_model.CpModel()
    farmacias_domingo = ["San josé Parque", "Amatitlan", "Alioto"]

    # 1. Variables de turnos
    shifts = {}
    for e in empleados:
        for f in farmacias:
            for d in range(dias):
                shifts[(e['id'], f['id'], d)] = model.NewBoolVar(f"shift_e{e['id']}_f{f['id']}_d{d}")

    # R1: Un empleado solo puede trabajar en una farmacia por día (o ninguna)
    for e in empleados:
        for d in range(dias):
            model.Add(sum(shifts[(e['id'], f['id'], d)] for f in farmacias) <= 1)

    # R2: Máximo 6 días por semana por empleado
    # Cumplimiento Art. 126 Código de Trabajo Guatemala - Descanso semanal obligatorio
    for e in empleados:
        total_dias = sum(shifts[(e['id'], f['id'], d)] for f in farmacias for d in range(dias))
        model.Add(total_dias <= 6)
        model.Add(total_dias >= 1)

    # R3: Permisos Aprobados (El empleado que pidió permiso NO trabaja ese día)
    for aus in ausencias:
        for f in farmacias:
            model.Add(shifts[(aus['empleado_id'], f['id'], aus['dia'])] == 0)

    # R4: Cierres en domingo (Nadie trabaja en farmacias cerradas en domingo)
    for f in farmacias:
        if f.get('nombre') not in farmacias_domingo:
            for e in empleados:
                model.Add(shifts[(e['id'], f['id'], 6)] == 0)

    # R5: Asignaciones Forzadas vs Farmacia Base (La regla más delicada)
    for e in empleados:
        if e['rol'] == 'Dependiente' and e['farmacia_id']:
            for d in range(dias):
                
                # Ver si el admin le forzó una farmacia específica a este empleado HOY
                orden_hoy = None
                for of in forzadas:
                    if of['empleado_id'] == e['id'] and of['dia'] == d:
                        orden_hoy = of
                        break

                if orden_hoy:
                    # El admin OBLIGA a que el empleado esté en esta farmacia destino hoy
                    for f in farmacias:
                        if f['id'] == orden_hoy['farmacia_dest_id']:
                            # Obligamos a que el turno sea 1, EXCEPTO si es domingo y la farmacia cierra.
                            # Para evitar conflicto con la R4
                            if d == 6 and f.get('nombre') not in farmacias_domingo:
                                model.Add(shifts[(e['id'], f['id'], d)] == 0) # El domingo pesa más
                            else:
                                model.Add(shifts[(e['id'], f['id'], d)] == 1)
                        else:
                            model.Add(shifts[(e['id'], f['id'], d)] == 0)
                else:
                    # Si NO hay orden del admin, el dependiente solo puede estar en su farmacia base
                    for f in farmacias:
                        if f['id'] != e['farmacia_id']:
                            model.Add(shifts[(e['id'], f['id'], d)] == 0)


    # R6: Lógica de Comodines (A prueba de fallos)
    # Extraemos dónde y cuándo faltará alguien (los huecos a cubrir)
    huecos_aprobados = []
    for a in ausencias:
        if a.get('farmacia_id') is not None:
            huecos_aprobados.append((a['farmacia_id'], a['dia']))

    for e in empleados:
        if e['rol'] == 'Comodin':
            for d in range(dias):
                
                # Ver si el admin OBLIGÓ al comodín a ir a algún lado hoy
                orden_comodin_hoy = None
                for of in forzadas:
                    if of['empleado_id'] == e['id'] and of['dia'] == d:
                        orden_comodin_hoy = of
                        break

                for f in farmacias:
                    if orden_comodin_hoy:
                        # Si el admin lo ordenó, el comodín TIENE que ir a esa farmacia (salvo cierre en domingo)
                        if f['id'] == orden_comodin_hoy['farmacia_dest_id']:
                            if d == 6 and f.get('nombre') not in farmacias_domingo:
                                model.Add(shifts[(e['id'], f['id'], d)] == 0)
                            else:
                                model.Add(shifts[(e['id'], f['id'], d)] == 1)
                        else:
                            model.Add(shifts[(e['id'], f['id'], d)] == 0)
                    else:
                        # Si NO hay orden del admin, el comodín SOLO puede cubrir permisos médicos/personales
                        if (f['id'], d) not in huecos_aprobados:
                            model.Add(shifts[(e['id'], f['id'], d)] == 0)

    # R7: Máximo 1 comodín por hueco (Para no mandar a todos a cubrir el mismo lugar)
    # Solo aplica para los comodines sin órdenes forzadas.
    comodines = [e for e in empleados if e['rol'] == 'Comodin']
    if comodines:
        for (f_id, d) in huecos_aprobados:
            model.Add(sum(shifts[(c['id'], f_id, d)] for c in comodines) <= 1)

    # R8: Descanso fijo programado (Ej. Universidad o Religión)
    # Cumplimiento Art. 126 Código de Trabajo Guatemala - Descanso semanal obligatorio
    for e in empleados:
        if e.get('dia_descanso_fijo') is not None:
            d_fijo = e['dia_descanso_fijo']
            for f in farmacias:
                # El modelo fuerza a que el turno en ese día específico sea 0
                model.Add(shifts[(e['id'], f['id'], d_fijo)] == 0)

    # OBJETIVO: Maximizar la cobertura total
    cobertura_total = []
    for f in farmacias:
        for d in range(dias):
            # No contamos las farmacias cerradas en domingo para el objetivo
            if d == 6 and f.get('nombre') not in farmacias_domingo:
                continue
            cobertura_total.append(sum(shifts[(e['id'], f['id'], d)] for e in empleados))

    model.Maximize(sum(cobertura_total))

    # Ejecutar Solver
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5.0

    status = solver.Solve(model)
    print("STATUS OR-TOOLS:", status) # Esto te ayudará en la consola

    if status not in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
        print("No se encontró solución factible.")
        return None

    # Guardar resultados
    resultados = []
    for e in empleados:
        for f in farmacias:
            for d in range(dias):
                if solver.Value(shifts[(e['id'], f['id'], d)]) == 1:
                    resultados.append({
                        'empleado_id': e['id'],
                        'farmacia_id': f['id'],
                        'dia': d
                    })

    return resultados