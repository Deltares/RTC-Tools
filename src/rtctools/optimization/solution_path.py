import os
import glob
import shutil


class ExportResultsEachPriority:
    def priority_completed(self, priority):
        super().priority_completed(priority)

        self.write()
        # Move all output files to a priority-specific folder
        new_output_folder = os.path.join(self._output_folder, "priority{}".format(priority))
        os.makedirs(new_output_folder, exist_ok=True)
        output_file_stem = os.path.join(self._output_folder, self.timeseries_export_basename)
        for output_file in glob.glob(output_file_stem + ".*"):
            output_file_name = os.path.basename(output_file)
            shutil.copyfile(output_file, os.path.join(new_output_folder, output_file_name))
