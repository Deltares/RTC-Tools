import itertools

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime


data_path = "../../../examples/cascading_channels/output/timeseries_export.csv"

# Import Data
record = np.recfromcsv(data_path, encoding=None)

# Get times as datetime objects
datefunc = lambda x: datetime.strptime(x, "%Y-%m-%d %H:%M:%S")
times = list(map(datefunc, record["time"]))

channels = "UpperChannel", "MiddleChannel", "LowerChannel"

# Generate Plot
n_subplots = 3
f, axarr = plt.subplots(n_subplots, sharex=True, figsize=(8, 4 * n_subplots))
axarr[0].set_title("Water Levels and Flow Rates")

# Upper subplot
axarr[0].set_ylabel("Water Level [m]")
for c in channels:
    axarr[0].plot(
        times,
        record[c.lower() + "h1"],
        label=c + ".H[1]",
        linewidth=1,
        color="mediumblue",
    )
    axarr[0].plot(
        times,
        record[c.lower() + "h2"],
        label=c + ".H[2]",
        linewidth=1,
        color="mediumorchid",
    )
    axarr[0].plot(
        times,
        record[c.lower() + "h2_max"],
        label=c + ".H_max",
        linewidth=1,
        color="darkorange",
        linestyle="--",
    )
    axarr[0].plot(
        times,
        record[c.lower() + "h2_min"],
        label=c + ".H_min",
        linewidth=1,
        color="darkred",
        linestyle=":",
    )

# Middle Subplot
axarr[1].set_ylabel("Flow Rate [m³/s]")
axarr[1].plot(
    times,
    record["Inflow_Q".lower()],
    label="Inflow_Q",
    linewidth=1,
    color="mediumorchid",
)
axarr[1].plot(
    times,
    record["DrinkingWaterExtractionPump_Q_target".lower()],
    label="ExtractionPump_Q_target",
    linewidth=6,
    color="lightskyblue",
)
axarr[1].plot(
    times,
    record["DrinkingWaterExtractionPump_Q".lower()],
    label="ExtractionPump_Q",
    linewidth=1,
    color="mediumblue",
)
axarr[1].set_ylim(bottom=0)

# Lower Subplot
axarr[2].set_ylabel("Flow Rate [m³/s]")

axarr[2].plot(
    times,
    record["UpperControlStructure_Q".lower()],
    label="UpperControlStructure_Q",
    linewidth=1,
    color="darkred",
)
axarr[2].plot(
    times,
    record["LowerControlStructure_Q".lower()],
    label="LowerControlStructure_Q",
    linewidth=1,
    color="darkorange",
)
axarr[2].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
axarr[2].set_ylim(bottom=0)
f.autofmt_xdate()

# Shrink each axis by 20% and put a legend to the right of the axis
for i in range(n_subplots):
    box = axarr[i].get_position()
    axarr[i].set_position([box.x0, box.y0, box.width * 0.8, box.height])
    axarr[i].legend(
        loc="center left", bbox_to_anchor=(1, 0.5), frameon=False, prop={"size": 8}
    )

plt.autoscale(enable=True, axis='x', tight=True)

# Output Plot
plt.show()
