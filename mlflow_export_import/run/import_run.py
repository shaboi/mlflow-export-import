"""
Imports a run from a directory.
"""

import os
import tempfile
import click
import base64

import mlflow
from mlflow.entities.lifecycle_stage import LifecycleStage
from mlflow.entities import RunStatus
from mlflow.utils.mlflow_tags import MLFLOW_PARENT_RUN_ID

from mlflow_export_import.common.click_options import (
    opt_input_dir,
    opt_import_source_tags,
    opt_experiment_name,
    opt_use_src_user_id,
    opt_dst_notebook_dir
)
from mlflow_export_import.common import utils, mlflow_utils, io_utils
from mlflow_export_import.common.filesystem import mk_local_path
from mlflow_export_import.common.find_artifacts import find_run_model_names
from mlflow_export_import.common import filesystem as _filesystem
from mlflow_export_import.common import MlflowExportImportException
from mlflow_export_import.client.http_client import DatabricksHttpClient
from mlflow_export_import.run import run_data_importer

_logger = utils.getLogger(__name__)

def import_run(
        input_dir,
        experiment_name,
        import_source_tags = False,
        dst_notebook_dir = None,
        use_src_user_id = False,
        mlmodel_fix = True,
        mlflow_client = None
    ):
    """
    Imports a run into the specified experiment.
    :param experiment_name: Experiment name to add the run to.
    :param input_dir: Directory that contains the exported run.
    :param dst_notebook_dir: Databricks destination workpsace directory for notebook.
    :param import_source_tags: Import source information for MLFlow objects and create tags in destination object.
    :param mlmodel_fix: Add correct run ID in destination MLmodel artifact.
                        Can be expensive for deeply nested artifacts.
    :param use_src_user_id: Set the destination user ID to the source user ID.
                            Source user ID is ignored when importing into
                            Databricks since setting it is not allowed.
    :param dst_notebook_dir: Databricks destination workspace directory for notebook import.
    :param mlflow_client: MLflow client.
    :return: The run and its parent run ID if the run is a nested run.
    """

    def _mk_ex(src_run_dct, dst_run_id, exp_name):
        return { "message": "Cannot import run", 
            "src_run_dct": src_run_dct["info"].get("run_id",None), 
            "dst_run_dct": dst_run_id,
            "experiment": exp_name
    }

    mlflow_client = mlflow_client or mlflow.MlflowClient()
    dbx_client = DatabricksHttpClient(mlflow_client.tracking_uri)

    #_logger.debug(f"importing_into_databricks: {utils.importing_into_databricks()}")
    _logger.info(f"Importing run from '{input_dir}'")

    exp = mlflow_utils.set_experiment(mlflow_client, dbx_client, experiment_name)
    src_run_path = os.path.join(input_dir, "run.json")
    src_run_dct = io_utils.read_file_mlflow(src_run_path)
    in_databricks = "DATABRICKS_RUNTIME_VERSION" in os.environ
    #_logger.debug(f"in_databricks: {in_databricks}")

    run = mlflow_client.create_run(exp.experiment_id)
    run_id = run.info.run_id
    try:
        run_data_importer.import_run_data(
            mlflow_client,
            src_run_dct,
            run_id,
            import_source_tags,
            src_run_dct["info"]["user_id"],
            use_src_user_id,
            in_databricks
        )
        path = os.path.join(input_dir, "artifacts")
        if os.path.exists(_filesystem.mk_local_path(path)):
            mlflow_client.log_artifacts(run_id, mk_local_path(path))
        if mlmodel_fix:
            _update_mlmodel_run_id(mlflow_client, run_id)
        mlflow_client.set_terminated(run_id, RunStatus.to_string(RunStatus.FINISHED))
        run = mlflow_client.get_run(run_id)
        if src_run_dct["info"]["lifecycle_stage"] == LifecycleStage.DELETED:
            mlflow_client.delete_run(run.info.run_id)
            run = mlflow_client.get_run(run.info.run_id)
    except Exception as e:
        mlflow_client.set_terminated(run_id, RunStatus.to_string(RunStatus.FAILED))
        import traceback
        traceback.print_exc()
        raise MlflowExportImportException(e, f"Importing run {run_id} of experiment '{exp.name}' failed")

    if utils.importing_into_databricks() and dst_notebook_dir:
        _upload_databricks_notebook(dbx_client, input_dir, src_run_dct, dst_notebook_dir)

    res = (run, src_run_dct["tags"].get(MLFLOW_PARENT_RUN_ID, None))
    _logger.info(f"Imported run '{run.info.run_id}' into experiment '{experiment_name}'")
    return res


def _update_mlmodel_run_id(mlflow_client, run_id):
    """
    Workaround to fix the run_id in the destination MLmodel file since there is no method to get all model artifacts of a run.
    Since an MLflow run does not keep track of its models, there is no method to retrieve the artifact path to all its models.
    This workaround recursively searches the run's root artifact directory for all MLmodel files, and assumes their directory
    represents a path to the model.
    """
    mlmodel_paths = find_run_model_names(run_id)
    for model_path in mlmodel_paths:
        download_uri = f"runs:/{run_id}/{model_path}/MLmodel"
        local_path = mlflow_utils.download_artifacts(mlflow_client, download_uri)
        mlmodel = io_utils.read_file(local_path, "yaml")
        mlmodel["run_id"] = run_id
        with tempfile.TemporaryDirectory() as dir:
            output_path = os.path.join(dir, "MLmodel")
            io_utils.write_file(output_path, mlmodel, "yaml")
            if model_path == "MLmodel":
                model_path = ""
            mlflow_client.log_artifact(run_id, output_path, model_path)


def _upload_databricks_notebook(dbx_client, input_dir, src_run_dct, dst_notebook_dir):
    run_id = src_run_dct["info"]["run_id"]
    tag_key = "mlflow.databricks.notebookPath"
    src_notebook_path = src_run_dct["tags"].get(tag_key,None)
    if not src_notebook_path:
        _logger.warning(f"No tag '{tag_key}' for run_id '{run_id}'")
        return
    notebook_name = os.path.basename(src_notebook_path)

    format = "source"
    notebook_path = os.path.join(input_dir,"artifacts","notebooks",f"{notebook_name}.{format}")
    if not os.path.exists(notebook_path):
        _logger.warning(f"Source '{notebook_path}' does not exist for run_id '{run_id}'")
        return

    with open(notebook_path, "r", encoding="utf-8") as f:
        content = f.read()
    dst_notebook_path = os.path.join(dst_notebook_dir, notebook_name)
    content = base64.b64encode(content.encode()).decode("utf-8")
    data = {
        "path": dst_notebook_path,
        "language": "PYTHON",
        "format": format,
        "overwrite": True,
        "content": content
        }
    mlflow_utils.create_workspace_dir(dbx_client, dst_notebook_dir)
    try:
        _logger.info(f"Importing notebook '{dst_notebook_path}' for run {run_id}")
        dbx_client._post("workspace/import", data)
    except MlflowExportImportException as e:
        _logger.warning(f"Cannot save notebook '{dst_notebook_path}'. {e}")


@click.command()
@opt_input_dir
@opt_experiment_name
@opt_import_source_tags
@opt_use_src_user_id
@opt_dst_notebook_dir
@click.option("--mlmodel-fix",
    help="Add correct run ID in destination MLmodel artifact. Can be expensive for deeply nested artifacts.",
    type=bool,
    default=True,
    show_default=True
)

def main(input_dir,
        experiment_name,
        import_source_tags,
        mlmodel_fix,
        use_src_user_id,
        dst_notebook_dir
    ):
    _logger.info("Options:")
    for k,v in locals().items():
        _logger.info(f"  {k}: {v}")
    import_run(
        input_dir = input_dir,
        experiment_name = experiment_name,
        import_source_tags = import_source_tags,
        dst_notebook_dir = dst_notebook_dir,
        use_src_user_id = use_src_user_id,
        mlmodel_fix = mlmodel_fix
    )


if __name__ == "__main__":
    main()
