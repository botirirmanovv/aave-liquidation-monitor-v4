// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {MarketParams, IMorphoLiquidateCallback} from "../../contracts/MorphoFlashLiquidator.sol";

interface IERC20Mint {
    function mint(address to, uint256 amount) external;
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address owner) external view returns (uint256);
}

/// @dev Mirrors Morpho Blue liquidate ordering: send collateral → callback → pull repay.
contract MockMorphoBlue {
    error InconsistentInput();

    uint256 public bonusBps = 10500; // 5% liquidation bonus, test-friendly

    function setBonusBps(uint256 value) external {
        bonusBps = value;
    }

    function liquidate(
        MarketParams memory marketParams,
        address,
        uint256 seizedAssets,
        uint256 repaidShares,
        bytes memory data
    ) external returns (uint256 seized, uint256 repaidAssets) {
        if ((seizedAssets == 0) == (repaidShares == 0)) revert InconsistentInput();

        if (seizedAssets > 0) {
            seized = seizedAssets;
            repaidAssets = (seizedAssets * 10_000) / bonusBps;
        } else {
            repaidAssets = repaidShares;
            seized = (repaidAssets * bonusBps) / 10_000;
        }

        IERC20Mint(marketParams.collateralToken).transfer(msg.sender, seized);
        if (data.length > 0) {
            IMorphoLiquidateCallback(msg.sender).onMorphoLiquidate(repaidAssets, data);
        }
        IERC20Mint(marketParams.loanToken).transferFrom(msg.sender, address(this), repaidAssets);
    }
}

contract MockERC20Lite {
    string public name;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    constructor(string memory _name) {
        name = _name;
    }

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _move(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "allowance");
        if (allowed != type(uint256).max) {
            allowance[from][msg.sender] = allowed - amount;
        }
        _move(from, to, amount);
        return true;
    }

    function _move(address from, address to, uint256 amount) internal {
        require(balanceOf[from] >= amount, "balance");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
    }
}

/// @dev Generic venue — tests encode swapAll as swapCalldata (not hardcoded UniV2 in the bot).
contract MockSwapRouter {
    uint256 public rateBps = 10_000;
    bool public fail;

    function setRateBps(uint256 value) external {
        rateBps = value;
    }

    function setFail(bool value) external {
        fail = value;
    }

    function swapAll(address tokenIn, address tokenOut, uint256 minOut) external returns (uint256 amountOut) {
        if (fail) revert("swap-fail");
        uint256 amountIn = MockERC20Lite(tokenIn).balanceOf(msg.sender);
        require(amountIn > 0, "empty");
        MockERC20Lite(tokenIn).transferFrom(msg.sender, address(this), amountIn);
        amountOut = (amountIn * rateBps) / 10_000;
        require(amountOut >= minOut, "slippage");
        MockERC20Lite(tokenOut).transfer(msg.sender, amountOut);
    }
}
