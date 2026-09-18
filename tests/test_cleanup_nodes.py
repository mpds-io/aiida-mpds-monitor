"""Verify destructive cleanup using mocked API and database connections."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("yascheduler.entrypoints.config_parser")
pytest.importorskip("hcloud")
pytest.importorskip("pg8000")

import cleanup_nodes as cleanup


def server(server_id, ip, name="yascheduler-test"):
    ipv4 = SimpleNamespace(ip=ip) if ip else None
    return SimpleNamespace(
        id=server_id, name=name, public_net=SimpleNamespace(ipv4=ipv4)
    )


def node(node_id, hostname, external_id=None, enabled=True, cloud="hetzner", tasks=0):
    return cleanup.SchedulerNode(node_id, hostname, external_id, cloud, enabled, tasks)


def test_only_unmatched_nodes_are_selected_including_disabled_rows():
    live = server(11, "192.0.2.1")
    orphan = server(12, "192.0.2.2")
    registered = node(1, "192.0.2.1", enabled=False)
    stale = node(2, "192.0.2.3", enabled=False)
    unrelated_row = node(3, "192.0.2.4", cloud="az")
    assert cleanup.find_leftovers(
        [live, orphan], [registered, stale, unrelated_row], "yascheduler-"
    ) == ([orphan], [stale])


def test_external_id_protects_server_when_ip_changes():
    live = server(11, "192.0.2.2")
    assert cleanup.find_leftovers(
        [live], [node(1, "192.0.2.1", "11")], "yascheduler-"
    ) == ([], [])


def test_server_without_ipv4_matches_by_external_id():
    live = server(11, None)
    assert cleanup.find_leftovers([live], [node(1, None, "11")], "yascheduler-") == (
        [],
        [],
    )


def test_unrelated_project_servers_are_excluded_unless_requested():
    unrelated = server(11, "192.0.2.1", "database")
    assert cleanup.find_leftovers([unrelated], [], "yascheduler-") == ([], [])
    assert cleanup.find_leftovers([unrelated], [], None) == ([unrelated], [])


def test_other_cloud_registration_also_protects_api_server():
    live = server(11, "192.0.2.1")
    assert cleanup.find_leftovers(
        [live], [node(1, "192.0.2.1", cloud="az")], "yascheduler-"
    ) == ([], [])


def test_unidentifiable_rows_are_skipped():
    assert cleanup.find_leftovers([], [node(1, None)], "yascheduler-") == ([], [])


@pytest.fixture
def backend(monkeypatch, tmp_path):
    # Exercise the installed yascheduler parser, with dummy credentials only.
    config_path = tmp_path / "yascheduler.conf"
    config_path.write_text(
        f"[local]\ndata_dir = {tmp_path}\n"
        "[db]\nuser = checker\npassword = dummy\ndatabase = scheduler\n"
        "host = localhost\nport = 5432\n"
        "[clouds]\nhetzner_token = dummy\nhetzner_label = yascheduler\n"
    )
    orphan = server(12, "192.0.2.2")
    stale = node(2, "192.0.2.3", tasks=1)
    client = Mock()
    client.servers.get_all.return_value = [orphan]
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchall.side_effect = [
        [(stale.node_id, stale.hostname, None, "hetzner", True, 1)],
        [(99,)],
    ]
    monkeypatch.setattr(cleanup, "HetznerClient", Mock(return_value=client))
    monkeypatch.setattr(cleanup.pg8000, "connect", Mock(return_value=connection))
    return SimpleNamespace(
        argv=["--config", str(config_path)],
        client=client,
        connection=connection,
        cursor=cursor,
        orphan=orphan,
    )


def test_preview_performs_no_deletions_or_commits(backend, capsys):
    assert cleanup.main(backend.argv) == 0
    backend.client.servers.delete.assert_not_called()
    backend.connection.commit.assert_not_called()
    assert len(backend.cursor.execute.call_args_list) == 1
    backend.connection.close.assert_called_once()
    backend.cursor.close.assert_called_once()
    output = capsys.readouterr().out
    assert "API leftovers: 1" in output
    assert "Scheduler leftovers: 1" in output
    assert "running_tasks=1" in output
    assert "Preview only" in output


def test_apply_abandons_tasks_before_deleting_and_commits(backend, capsys):
    assert cleanup.main(backend.argv + ["--apply"]) == 0
    calls = backend.cursor.execute.call_args_list
    assert "LOCK TABLE" in calls[1].args[0]
    assert calls[3].args == (
        "UPDATE yascheduler_tasks SET status='DONE', error='node is gone' "
        "WHERE allocated_node_id=%s AND status='RUNNING' RETURNING task_id;",
        (2,),
    )
    assert calls[4].args == (
        "DELETE FROM yascheduler_nodes WHERE node_id=%s AND cloud='hetzner';",
        (2,),
    )
    backend.client.servers.delete.assert_called_once_with(backend.orphan)
    backend.client.servers.delete.return_value.wait_until_finished.assert_called_once()
    backend.connection.commit.assert_called_once()
    backend.connection.rollback.assert_not_called()
    assert "marked tasks DONE with error: [99]" in capsys.readouterr().out


def test_api_failure_is_reported_and_scheduler_cleanup_commits(backend, capsys):
    backend.client.servers.delete.side_effect = RuntimeError("API unavailable")
    assert cleanup.main(backend.argv + ["--apply"]) == 1
    backend.connection.commit.assert_called_once()
    assert "API deletion failed server_id=12" in capsys.readouterr().err


def test_deletion_action_failure_is_reported(backend):
    backend.client.servers.delete.return_value.wait_until_finished.side_effect = (
        RuntimeError("action failed")
    )
    assert cleanup.main(backend.argv + ["--apply"]) == 1


def test_db_failure_rolls_back_and_does_not_delete_api_servers(backend):
    def execute(sql, *args):
        if sql.startswith("DELETE"):
            raise RuntimeError("database deletion failed")

    backend.cursor.execute.side_effect = execute
    with pytest.raises(RuntimeError, match="database deletion failed"):
        cleanup.main(backend.argv + ["--apply"])
    backend.connection.rollback.assert_called_once()
    backend.connection.commit.assert_not_called()
    backend.client.servers.delete.assert_not_called()
    backend.connection.close.assert_called_once()


def test_api_listing_failure_cannot_trigger_cleanup(backend):
    backend.client.servers.get_all.side_effect = RuntimeError("listing failed")
    with pytest.raises(RuntimeError, match="listing failed"):
        cleanup.main(backend.argv + ["--apply"])
    backend.connection.rollback.assert_called_once()
    backend.client.servers.delete.assert_not_called()
    assert not any(
        call.args[0].startswith(("UPDATE", "DELETE"))
        for call in backend.cursor.execute.call_args_list
    )
