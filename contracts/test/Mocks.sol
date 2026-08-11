// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// Minimal stand-ins for Aave V3 Pool and a Uniswap V2 router, enough to drive
// AaveV3LiquidationBot through a complete flash-loan liquidation in-process.

interface IFlashLoanReceiver {
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}

contract MockERC20 {
    string public name;
    uint8 public constant decimals = 18;

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
        require(allowed >= amount, "MockERC20: allowance");
        if (allowed != type(uint256).max) {
            allowance[from][msg.sender] = allowed - amount;
        }
        _move(from, to, amount);
        return true;
    }

    function _move(address from, address to, uint256 amount) internal {
        require(balanceOf[from] >= amount, "MockERC20: balance");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
    }
}

contract MockAavePool {
    uint256 public healthFactor = 0.9e18;
    uint256 public premiumBps = 5;      // Aave charges 0.05% on flashLoanSimple
    uint256 public bonusBps = 10500;    // 5% liquidation bonus

    function setHealthFactor(uint256 value) external {
        healthFactor = value;
    }

    function setBonusBps(uint256 value) external {
        bonusBps = value;
    }

    function getUserAccountData(address)
        external
        view
        returns (uint256, uint256, uint256, uint256, uint256, uint256)
    {
        return (0, 0, 0, 0, 0, healthFactor);
    }

    /// @dev Mirrors Aave: hand over the funds, call back into the receiver
    /// synchronously, then pull principal + premium via transferFrom.
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16
    ) external {
        uint256 premium = (amount * premiumBps) / 10000;
        MockERC20(asset).transfer(receiverAddress, amount);

        bool ok = IFlashLoanReceiver(receiverAddress).executeOperation(
            asset, amount, premium, msg.sender, params
        );
        require(ok, "MockAavePool: callback returned false");

        MockERC20(asset).transferFrom(receiverAddress, address(this), amount + premium);
    }

    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address,
        uint256 debtToCover,
        bool
    ) external {
        MockERC20(debtAsset).transferFrom(msg.sender, address(this), debtToCover);
        uint256 seized = (debtToCover * bonusBps) / 10000;
        MockERC20(collateralAsset).transfer(msg.sender, seized);
    }
}

contract MockRouter {
    uint256 public rateBps = 10000; // 1:1 by default

    function setRateBps(uint256 value) external {
        rateBps = value;
    }

    function getAmountsOut(uint256 amountIn, address[] calldata path)
        external
        view
        returns (uint256[] memory amounts)
    {
        amounts = new uint256[](path.length);
        amounts[0] = amountIn;
        uint256 current = amountIn;
        for (uint256 i = 1; i < path.length; i++) {
            current = (current * rateBps) / 10000;
            amounts[i] = current;
        }
    }

    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory) {
        require(deadline >= block.timestamp, "MockRouter: expired");
        MockERC20(path[0]).transferFrom(msg.sender, address(this), amountIn);

        uint256 amountOut = (amountIn * rateBps) / 10000;
        require(amountOut >= amountOutMin, "MockRouter: slippage");
        MockERC20(path[path.length - 1]).transfer(to, amountOut);

        uint256[] memory amounts = new uint256[](path.length);
        amounts[0] = amountIn;
        amounts[path.length - 1] = amountOut;
        return amounts;
    }
}
