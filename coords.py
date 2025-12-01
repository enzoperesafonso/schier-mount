




def degrees_to_steps(angle_deg, steps_per_deg, zero_point):
    # Formula from C code: (angle * deg2enc) + zeropt
    return int(angle_deg * steps_per_deg) + zero_point