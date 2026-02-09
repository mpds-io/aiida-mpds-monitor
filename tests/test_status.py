from unittest.mock import MagicMock

from aiida_mpds_monitor.status import (
    STATUS_DONE,
    STATUS_EXC,
    STATUS_WAITING,
    check_child_calculation,
    get_node_status,
)


class TestCheckChildCalculation:
    """Test cases for check_child_calculation function."""

    def test_no_child_calculations(self):
        """Test when there are no child CrystalParallelCalculation nodes."""
        base_node = MagicMock()
        base_node.called = []

        result = check_child_calculation(base_node)

        assert result is False

    def test_child_calculation_succeeded(self):
        """Test when child calculation succeeded."""
        child_node = MagicMock()
        child_node.process_label = "CrystalParallelCalculation"
        child_node.pk = 1
        child_node.is_failed = False
        child_node.is_excepted = False
        child_node.is_killed = False

        base_node = MagicMock()
        base_node.called = [child_node]

        result = check_child_calculation(base_node)

        assert result is False

    def test_child_calculation_failed(self):
        """Test when child calculation failed."""
        child_node = MagicMock()
        child_node.process_label = "CrystalParallelCalculation"
        child_node.pk = 1
        child_node.is_failed = True
        child_node.is_excepted = False
        child_node.is_killed = False

        base_node = MagicMock()
        base_node.pk = 100
        base_node.called = [child_node]

        result = check_child_calculation(base_node)

        assert result is True

    def test_child_calculation_excepted(self):
        """Test when child calculation excepted."""
        child_node = MagicMock()
        child_node.process_label = "CrystalParallelCalculation"
        child_node.pk = 1
        child_node.is_failed = False
        child_node.is_excepted = True
        child_node.is_killed = False

        base_node = MagicMock()
        base_node.pk = 100
        base_node.called = [child_node]

        result = check_child_calculation(base_node)

        assert result is True

    def test_child_calculation_killed(self):
        """Test when child calculation killed."""
        child_node = MagicMock()
        child_node.process_label = "CrystalParallelCalculation"
        child_node.pk = 1
        child_node.is_failed = False
        child_node.is_excepted = False
        child_node.is_killed = True

        base_node = MagicMock()
        base_node.pk = 100
        base_node.called = [child_node]

        result = check_child_calculation(base_node)

        assert result is True

    def test_multiple_child_calculations_last_failed(self):
        """Test with multiple child calculations, last one failed."""
        child1 = MagicMock()
        child1.process_label = "CrystalParallelCalculation"
        child1.pk = 1
        child1.is_failed = False
        child1.is_excepted = False
        child1.is_killed = False

        child2 = MagicMock()
        child2.process_label = "CrystalParallelCalculation"
        child2.pk = 2
        child2.is_failed = True
        child2.is_excepted = False
        child2.is_killed = False

        base_node = MagicMock()
        base_node.pk = 100
        base_node.called = [child1, child2]

        result = check_child_calculation(base_node)

        assert result is True

    def test_multiple_child_calculations_last_succeeded(self):
        """Test with multiple child calculations, last one succeeded."""
        child1 = MagicMock()
        child1.process_label = "CrystalParallelCalculation"
        child1.pk = 1
        child1.is_failed = True
        child1.is_excepted = False
        child1.is_killed = False

        child2 = MagicMock()
        child2.process_label = "CrystalParallelCalculation"
        child2.pk = 2
        child2.is_failed = False
        child2.is_excepted = False
        child2.is_killed = False

        base_node = MagicMock()
        base_node.pk = 100
        base_node.called = [child1, child2]

        result = check_child_calculation(base_node)

        assert result is False

    def test_exception_handling(self):
        """Test exception handling in check_child_calculation."""
        base_node = MagicMock()
        base_node.called = None  # This will cause an exception

        result = check_child_calculation(base_node)

        assert result is False

    def test_with_logger(self):
        """Test logging when child calculation failed."""
        logger = MagicMock()
        child_node = MagicMock()
        child_node.process_label = "CrystalParallelCalculation"
        child_node.pk = 1
        child_node.is_failed = True
        child_node.is_excepted = False
        child_node.is_killed = False

        base_node = MagicMock()
        base_node.pk = 100
        base_node.called = [child_node]

        result = check_child_calculation(base_node, logger=logger)

        assert result is True
        logger.warning.assert_called_once()


class TestGetNodeStatus:
    """Test cases for get_node_status function."""

    def test_finished_success(self):
        """Test finished node with success exit code."""
        node = MagicMock()
        node.process_state.value = "finished"
        node.is_excepted = False
        node.exit_code.status = 0
        node.called = []

        result = get_node_status(node)

        assert result == STATUS_DONE

    def test_finished_with_error_code(self):
        """Test finished node with non-zero exit code."""
        node = MagicMock()
        node.process_state.value = "finished"
        node.is_excepted = False
        node.exit_code.status = 1
        node.is_failed = True
        node.called = []

        result = get_node_status(node)

        assert result == "excepted-1"

    def test_finished_excepted_not_failed(self):
        """Test finished node that excepted but is not marked as failed."""
        node = MagicMock()
        node.process_state.value = "finished"
        node.is_excepted = True
        node.exit_code = None
        node.is_failed = False
        node.called = []

        result = get_node_status(node)

        assert result == STATUS_EXC

    def test_running_status(self):
        """Test running node status."""
        node = MagicMock()
        node.process_state.value = "running"

        result = get_node_status(node)

        assert result == STATUS_WAITING

    def test_submitting_status(self):
        """Test submitting node status."""
        node = MagicMock()
        node.process_state.value = "submitting"

        result = get_node_status(node)

        assert result == STATUS_WAITING

    def test_created_status(self):
        """Test created node status."""
        node = MagicMock()
        node.process_state.value = "created"

        result = get_node_status(node)

        assert result == STATUS_WAITING

    def test_excepted_state(self):
        """Test excepted state."""
        node = MagicMock()
        node.process_state.value = "excepted"
        node.exit_code.status = 1

        result = get_node_status(node)

        assert result == "excepted-1"

    def test_excepted_state_no_exit_code(self):
        """Test excepted state without exit code."""
        node = MagicMock()
        node.process_state.value = "excepted"
        node.exit_code = None

        result = get_node_status(node)

        assert result == "excepted-1"

    def test_unknown_state(self):
        """Test unknown state."""
        node = MagicMock()
        node.process_state.value = "unknown"

        result = get_node_status(node)

        assert result == STATUS_EXC

    def test_finished_with_failed_child(self):
        """Test finished node with failed child calculation."""
        child_node = MagicMock()
        child_node.process_label = "CrystalParallelCalculation"
        child_node.pk = 1
        child_node.is_failed = True
        child_node.is_excepted = False
        child_node.is_killed = False

        node = MagicMock()
        node.process_state.value = "finished"
        node.is_excepted = False
        node.exit_code.status = 0
        node.pk = 100
        node.called = [child_node]

        result = get_node_status(node)

        assert result == STATUS_EXC


class TestCheckChildCalculationWithCustomTypes:
    """Test cases for check_child_calculation with custom child types."""

    def test_custom_child_type_found_failed(self):
        """Test with custom child type that failed."""
        child_node = MagicMock()
        child_node.process_label = "CustomChildType"
        child_node.pk = 1
        child_node.is_failed = True
        child_node.is_excepted = False
        child_node.is_killed = False

        base_node = MagicMock()
        base_node.pk = 100
        base_node.called = [child_node]

        result = check_child_calculation(
            base_node, child_types=["CustomChildType"]
        )

        assert result is True

    def test_custom_child_type_not_found_when_different_type_exists(self):
        """Test when different child type exists but we're looking for another."""
        child_node = MagicMock()
        child_node.process_label = "SomeOtherType"
        child_node.pk = 1
        child_node.is_failed = True
        child_node.is_excepted = False
        child_node.is_killed = False

        base_node = MagicMock()
        base_node.called = [child_node]

        result = check_child_calculation(
            base_node, child_types=["CustomChildType"]
        )

        assert result is False

    def test_multiple_custom_child_types(self):
        """Test with multiple custom child types."""
        child1 = MagicMock()
        child1.process_label = "Type1"
        child1.pk = 1
        child1.is_failed = False
        child1.is_excepted = False
        child1.is_killed = False

        child2 = MagicMock()
        child2.process_label = "Type2"
        child2.pk = 2
        child2.is_failed = True
        child2.is_excepted = False
        child2.is_killed = False

        base_node = MagicMock()
        base_node.pk = 100
        base_node.called = [child1, child2]

        # Looking for both types - should find Type2 as failed
        result = check_child_calculation(
            base_node, child_types=["Type1", "Type2"]
        )

        assert result is True

    def test_custom_child_type_with_empty_list(self):
        """Test with empty child_types list."""
        child_node = MagicMock()
        child_node.process_label = "CrystalParallelCalculation"
        child_node.pk = 1
        child_node.is_failed = True
        child_node.is_excepted = False
        child_node.is_killed = False

        base_node = MagicMock()
        base_node.called = [child_node]

        # Should return False because we're not looking for this type
        result = check_child_calculation(base_node, child_types=[])

        assert result is False


class TestGetNodeStatusWithCustomTypes:
    """Test cases for get_node_status with custom child types."""

    def test_finished_with_custom_child_type_failed(self):
        """Test finished node with custom child type failed."""
        child_node = MagicMock()
        child_node.process_label = "CustomCalculation"
        child_node.pk = 1
        child_node.is_failed = True
        child_node.is_excepted = False
        child_node.is_killed = False

        node = MagicMock()
        node.process_state.value = "finished"
        node.is_excepted = False
        node.exit_code.status = 0
        node.pk = 100
        node.called = [child_node]

        result = get_node_status(node, child_types=["CustomCalculation"])

        assert result == STATUS_EXC

    def test_finished_with_custom_child_type_succeeded(self):
        """Test finished node with custom child type that succeeded."""
        child_node = MagicMock()
        child_node.process_label = "CustomCalculation"
        child_node.pk = 1
        child_node.is_failed = False
        child_node.is_excepted = False
        child_node.is_killed = False

        node = MagicMock()
        node.process_state.value = "finished"
        node.is_excepted = False
        node.exit_code.status = 0
        node.pk = 100
        node.called = [child_node]

        result = get_node_status(node, child_types=["CustomCalculation"])

        assert result == STATUS_DONE

    def test_default_child_type_used_when_none_provided(self):
        """Test that default CrystalParallelCalculation is used when child_types is None."""
        child_node = MagicMock()
        child_node.process_label = "CrystalParallelCalculation"
        child_node.pk = 1
        child_node.is_failed = True
        child_node.is_excepted = False
        child_node.is_killed = False

        node = MagicMock()
        node.process_state.value = "finished"
        node.is_excepted = False
        node.exit_code.status = 0
        node.pk = 100
        node.called = [child_node]

        result = get_node_status(node, child_types=None)

        assert result == STATUS_EXC
